#!/usr/bin/env python3
"""LineATA daemon v2: standing-read + event-driven host model.

Correct architecture per golden capture 2026-11-23:
- The host does NOT poll during idle. The dongle pushes events over the
  bulk-in endpoint; the host keeps a standing read, otherwise silence.
- All poll/walk ops belong to event-processing rituals, not idle.
- Events:
    64 xx  port phys change (phone plugged/unplugged) -> phys ritual
    a5 xx  hook change -> refresh + tone-gate templates
    a4 xx  dtmf digit  -> 67 04 31 77 + tone gate
"""
import argparse
import os
import sys
import time
import threading
import glob
import termios

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import linearusb as L
import initctl
import events as EV
import handlers
import usb.core


def h(items):
    return [bytes.fromhex(x) for x in items]


class Daemon:
    def __init__(self, verbose=True):
        self.verbose = verbose
        self.lin = L.Linear()
        self.running = True
        self.dispatch_lock = threading.Lock()
        self.hooks_seen = 0
        self.inbox = []

    def bring_up(self):
        inbox = []
        n_ok, n_acked = initctl.replay(self.lin, inbox=inbox)
        self.inbox = inbox
        self.log(f"init {n_ok}/{n_acked} (buffered {len(inbox)} frames)")
        # post-init transition (golden ops 86..119): mute markers + walk +
        # hook reg reads + final mute - runs ONCE after init, then silence
        for op in initctl.TRANSITION_POSTINIT:
            try:
                self.lin.w(h([op])[0])
                r = self.lin.r(1000, 8)
                if r:
                    self.inbox.append(bytes(r))
            except Exception as e:
                self.log(f"transition write {op[:16]}: {e}")
                raise
        self.log("transition done; entering standing-read idle")
        if getattr(self, "arm_after_transition", None):
            self.log(f"arming: {len(self.arm_after_transition)} ops")
            for idx, hx in enumerate(self.arm_after_transition):
                try:
                    self.lin.w(h([hx])[0])
                except Exception as e:
                    self.log(f"ARM FAILED at op {idx} [{hx[:16]}...]: {e}")
                    return False
                try:
                    self.lin.r(1000, 12)
                except Exception:
                    pass
            self.log("arming sent")

    def log(self, s):
        if self.verbose:
            print(f"[{time.strftime('%H:%M:%S')}] {s}", flush=True)

    def send_seq(self, seq, per_drain_ms=8):
        for op in h(seq):
            try:
                self.lin.w(op)
            except Exception as e:
                self.log(f"handler write {op.hex()[:16]}: {e}")
                return
            try:
                self.lin.r(1000, per_drain_ms)
            except Exception:
                pass

    def dispatch(self, frame: bytes):
        d = EV.decode(frame)
        if isinstance(d, EV.HookEvent):
            self.hooks_seen += 1
            self.log(f"HOOK port{d.port}: {'OFFHOOK' if d.offhook else 'ONHOOK'}")
            seq = handlers.on_offhook_a5_0100 if d.offhook else handlers.on_onhook_a5_0000
            with self.dispatch_lock:
                self.send_seq(seq)
        elif isinstance(d, EV.DtmfEvent):
            self.log(f"DTMF port{d.port}: {d.digit}")
            with self.dispatch_lock:
                self.send_seq(handlers.on_dtmf_digit)
        elif isinstance(d, EV.PhysEvent):
            self.log(f"PHYS port event: {frame.hex()}")
            with self.dispatch_lock:
                self.send_seq(handlers.on_phys_64)
        elif isinstance(d, EV.Unknown):
            self.log(f"msg {hex(d.cmd)} {d.payload.hex()[:28]}")

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

    def run(self):
        self.inbox = []
        self.bring_up()
        # process everything the dongle announced during bring-up
        phys_seen = False
        for frame in list(self.inbox):
            f = bytes(frame)
            if len(f) >= 2 and f[0] == 0x64:
                phys_seen = True
            self.dispatch(f)
        self.inbox = []
        # NOTE: phys re-registration ritual is state-dependent and NOT
        # byte-replayable (instant app fault when attempted). Port arming
        # needs the live-state reconstruction of nvox's PORT-PLUGGED handler;
        # static analysis pending. See PROTOCOL.md TODO.
        reader = threading.Thread(target=self.reader_loop, daemon=True)
        reader.start()
        try:
            while self.running:
                time.sleep(0.25)
        except KeyboardInterrupt:
            self.log("bye")
        self.running = False


# post-init transition: golden ops 86..119 (byte-exact)
initctl.TRANSITION_POSTINIT = []

def build_golden_init_and_transition():
    """Full init incl. the c6/c1/c0/c1 prologue + post-init transition,
    source-of-truth from the golden capture."""
    import re
    path = os.path.join(os.path.dirname(__file__), "..", "captures", "golden-call.log")
    if not os.path.exists(path):
        return None, None
    evs = []
    for ln in open(path, errors="replace"):
        m = re.match(r"^\[(\d+)\] (\S+) (.*)$", ln.strip())
        if m:
            evs.append((int(m.group(1)), m.group(2), m.group(3)))
    outs = [r.split(' ', 2)[2] for t, tag, r in evs if tag == 'S' and r.startswith('2 ')]
    init = outs[:outs.index("45081a0400000180") + 2]
    try:
        i1 = outs.index("45081a0400c01880")
        trans = outs[outs.index("45081a0400000180"):i1 + 2]
    except ValueError:
        trans = []
    return init, trans
    # init in golden = ops until the mute pair 45081a0400000180; find third
    # occurrence of mute markers pattern; transition = from first '45081a0400000180'
    try:
        i0 = outs.index("45081a0400000180")     # after init batch for port0/1
    except ValueError:
        return []
    # find the post-transition mutes: '45081a0400c01880'
    try:
        i1 = outs.index("45081a0400c01880")
    except ValueError:
        return []
    seq = outs[i0:i1 + 2]          # from 4508...0180 through 6508...1880 pair
    return seq


def main():
    ap = argparse.ArgumentParser()
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-init", action="store_true")
    ap.add_argument("--arm", default="", choices=["", "almost", "no-reset"],
                    help="experimental port arming variant after transition")
    args = ap.parse_args()
    d = usb.core.find(idVendor=0x31b7)
    if d is None:
        print("waiting for the device...", flush=True)
        while usb.core.find(idVendor=0x31b7) is None:
            time.sleep(1)
    if usb.core.find(idVendor=0x31b7).idProduct != 0x0001:
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
        print(f"post-init transition: {len(trans)} ops", flush=True)
    d = Daemon()
    if args.arm:
        import arming, handlers
        seq = arming.ARM_ALMOST if args.arm == "almost" else arming.ARM_NO_RESET
        d.log(f"arming ({args.arm}): {len(seq)} ops after transition")
        d.arm_after_transition = seq
    d.run()


if __name__ == "__main__":
    main()
