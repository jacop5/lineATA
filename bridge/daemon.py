#!/usr/bin/env python3
"""LineATA daemon v2: standing-read + event-driven host model.

Bring-up (golden sequence):
  cold boot (CDC 'boot') -> c6/c1 queries -> c0 SET COUNTRY (device
  re-enumerates on console-booted units; we wait & reclaim) -> remaining
  golden init ops -> 34-op post-init transition -> standing-read silence.
Events (dongle -> host pushes): 64 phys / a5 hook / a4 dtmf -> the
byte-exact handlers from the golden capture respond.
"""
import argparse
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import linearusb as L
import initctl
import events as EV
import handlers
import usb.core


def hx(items):
    return [bytes.fromhex(x if isinstance(x, str) else x[1]) for x in items]


class Daemon:
    def __init__(self, verbose=True):
        self.verbose = verbose
        self.lin = L.Linear()
        self.running = True
        self.dispatch_lock = threading.Lock()
        self.inbox = []
        self.arm_after_transition = None

    # ---------------- helpers ----------------
    def log(self, s):
        if self.verbose:
            print(f"[{time.strftime('%H:%M:%S')}] {s}", flush=True)

    def drain_into(self, inbox, ms=10):
        try:
            r = self.lin.r(1000, ms)
            if r:
                inbox.append(bytes(r))
        except usb.core.USBError:
            pass

    def send_seq(self, seq, per_drain_ms=8):
        for op in hx(seq):
            try:
                self.lin.w(op)
            except Exception as e:
                self.log(f"handler write {op.hex()[:16]}: {e}")
                return
            try:
                self.lin.r(1000, per_drain_ms)
            except Exception:
                pass

    # ---------------- bring-up ----------------
    def bring_up(self):
        inbox = []
        if initctl.GOLDEN_INIT and len(initctl.GOLDEN_INIT) > 3:
            self.bring_up_golden(inbox)
        else:
            n_ok, n_acked = initctl.replay(self.lin, inbox=inbox)
            self.log(f"init {n_ok}/{n_acked} (buffered {len(inbox)} frames)")
        # post-init transition
        for item in initctl.TRANSITION_POSTINIT:
            hexop = item if isinstance(item, str) else item[1]
            delay = 0 if isinstance(item, str) else item[0]
            if delay:
                time.sleep(min(delay, 200) / 1000.0)
            try:
                self.lin.w(bytes.fromhex(hexop))
                self.drain_into(inbox)
            except Exception as e:
                self.log(f"transition write {hexop[:14]}: {e}")
                raise
        self.log("transition done; entering standing-read idle")

    def bring_up_golden(self, inbox):
        seq = initctl.GOLDEN_INIT
        # 1) c6/c1 queries
        for item in seq[:2]:
            h = item if isinstance(item, str) else item[1]
            d = 0 if isinstance(item, str) else item[0]
            if d:
                time.sleep(d / 1000.0)
            self.lin.w(bytes.fromhex(h))
            self.drain_into(inbox)
        # 2) c0 SET COUNTRY: causes app re-init + USB re-enumeration
        c0item = seq[2]
        c0 = c0item if isinstance(c0item, str) else c0item[1]
        self.log(f"SET COUNTRY ({c0}) - device may re-enumerate")
        try:
            self.lin.w(bytes.fromhex(c0))
        except Exception:
            pass
        t0 = time.time()
        while time.time() - t0 < 12:
            d = usb.core.find(idVendor=0x31b7, idProduct=0x0001)
            if d and time.time() - t0 > 1.5:
                try:
                    self.lin = L.Linear()
                    self.log("device re-enumerated; reclaimed interface")
                    break
                except Exception:
                    time.sleep(0.4)
            time.sleep(0.25)
        # 3) remaining golden init ops with pacing
        n_ok = 0
        for item in seq[3:]:
            h = item if isinstance(item, str) else item[1]
            d = 0 if isinstance(item, str) else item[0]
            if d:
                time.sleep(min(d, 1500) / 1000.0)
            try:
                self.lin.w(bytes.fromhex(h))
                n_ok += 1
            except usb.core.USBError as e:
                self.log(f"golden init write failed at {h[:12]}: {e}")
                raise
            self.drain_into(inbox)
        self.log(f"golden init done ({n_ok} ops, buffered {len(inbox)} frames)")

    # ---------------- events ----------------
    def dispatch(self, frame: bytes):
        d = EV.decode(frame)
        if isinstance(d, EV.HookEvent):
            self.log(f"HOOK port{d.port}: {'OFFHOOK' if d.offhook else 'ONHOOK'}")
            with self.dispatch_lock:
                self.send_seq(handlers.on_offhook_a5_0100 if d.offhook
                              else handlers.on_onhook_a5_0000)
        elif isinstance(d, EV.DtmfEvent):
            self.log(f"DTMF port{d.port}: {d.digit}")
            with self.dispatch_lock:
                self.send_seq(handlers.on_dtmf_digit)
        elif isinstance(d, EV.PhysEvent):
            self.log(f"PHYS event: {frame.hex()}")
            with self.dispatch_lock:
                self.send_seq(handlers.on_phys_64)
        elif isinstance(d, EV.Unknown):
            self.log(f"msg {hex(d.cmd)} {d.payload.hex()[:24]}")

    def reader_loop(self):
        while self.running:
            try:
                data = self.lin.r(1000, 500)
            except usb.core.USBError as e:
                self.log(f"read gone: {e}")
                self.running = False
                return
            if not data:
                continue
            self.dispatch(bytes(data))

    # ---------------- main ----------------
    def run(self):
        self.bring_up()
        reader = threading.Thread(target=self.reader_loop, daemon=True)
        user_inbox, self.inbox = self.inbox, []
        reader.start()
        for frame in user_inbox:
            self.dispatch(bytes(frame))
        # optional experimental arming
        if getattr(self, "arm_after_transition", None):
            self.log(f"arming: {len(self.arm_after_transition)} ops")
            for idx, item in enumerate(self.arm_after_transition):
                h = item if isinstance(item, str) else item[1]
                try:
                    self.lin.w(bytes.fromhex(h))
                except Exception as e:
                    self.log(f"ARM FAILED at op {idx}: {e}")
                    break
                try:
                    self.lin.r(1000, 12)
                except Exception:
                    pass
            else:
                self.log("arming sent")
        try:
            while self.running:
                time.sleep(0.25)
        except KeyboardInterrupt:
            self.log("bye")
        self.running = False


def build_golden_init_and_transition():
    import re
    path = os.path.join(os.path.dirname(__file__), "..", "captures", "golden-call.log")
    if not os.path.exists(path):
        return None, None
    evs = []
    for ln in open(path, errors="replace"):
        m = re.match(r"^\[(\d+)\] (\S+) (.*)$", ln.strip())
        if m:
            evs.append((int(m.group(1)), m.group(2), m.group(3)))
    pairs = []
    last_t = None
    for t, tag, r in evs:
        if tag == 'S' and r.startswith('2 '):
            h2 = r.split(' ', 2)[2]
            d = 0 if last_t is None else max(0, t - last_t)
            pairs.append((d, h2))
            last_t = t
    outn = [h for d, h in pairs]
    init = pairs[:outn.index("45081a0400000180") + 2]
    try:
        i1 = outn.index("45081a0400c01880")
        trans = pairs[outn.index("45081a0400000180"):i1 + 2]
    except ValueError:
        trans = []
    return init, trans


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-init", action="store_true")
    ap.add_argument("--arm", default="", choices=["", "almost"],
                    help="experimental arming variant")
    args = ap.parse_args()
    d_dev = usb.core.find(idVendor=0x31b7)
    if d_dev is None:
        print("waiting for the device...", flush=True)
        while usb.core.find(idVendor=0x31b7) is None:
            time.sleep(1)
        d_dev = usb.core.find(idVendor=0x31b7)
    if d_dev.idProduct != 0x0001:
        print("cold-booting via CDC console...", flush=True)
        ok = L.cold_boot()
        print("boot:", ok, flush=True)
        if not ok:
            raise SystemExit("cold boot failed - replug the dongle (power cycle)")
    if not args.no_init:
        init, trans = build_golden_init_and_transition()
        if init:
            initctl.GOLDEN_INIT = init
            initctl.TRANSITION_POSTINIT = trans
            print(f"golden init: {len(init)} ops; transition: {len(trans)} ops", flush=True)
    d = Daemon()
    if args.arm == "almost":
        import arming
        d.arm_after_transition = arming.ARM_ALMOST
    d.run()


if __name__ == "__main__":
    main()
