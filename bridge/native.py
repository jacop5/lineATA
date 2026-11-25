#!/usr/bin/env python3
"""Native driver for the Keenetic Linear / KN-3110 FXS dongle.

Historical control-only prototype retained for comparison. It predates the
current ACK queue and error handling; use bridge.ata for current development.
Its readiness polling and port interpretations are not validated protocol facts.

Speaks the EFM32 mailbox protocol directly over USB bulk endpoints.
Init frames are a byte-exact replay of nvox 0.8.2.96 (see capture_init.py),
followed by the 1 Hz `a0 03 01` / `a0 03 00` heartbeat that keeps the
app's event channel alive.

Events arrive unprompted on EP 0x81 as:
    a5 04 <hook> 00     hook change, 1 = offhook, 0 = onhook
    a4 04 <digit> 00    DTMF digit (ASCII)
    64 ..               physical/line detect
"""
from __future__ import annotations

import os
import sys
import time
import glob
import termios
import select

import usb.core
import usb.util

try:
    from .capture_init import OPS
except ImportError:  # flat deployment
    from capture_init import OPS

VID = 0x31B7
PID_CDC = 0x0002
PID_LINEAR = 0x0001
EP_IN = 0x81
EP_OUT = 0x02

# Dial-tone / call setup frame nvox sends 7 ms after OFFHOOK (port offset 0x60
# selects port 2; low nibble/suffix bytes carry tone state). Byte-exact from
# the usbmon capture.
DIAL_TONE = (
    "6e5e0002317701001a0000008f0701001b000000260001001c00000000000000"
    "331f00003240000035000000340001001d000000000801001e00000000000100"
    "1f0000000000000037000000360000003900000038000000304600013101"
)
TONE_OFF = "67043177"


def _find_tty():
    for pat in ("/dev/ttyACM*", "/dev/cu.usbmodem*"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return None


def reset_to_bootloader(timeout=6.0):
    """Force the dongle back to the CDC bootloader (PID 0x0002)."""
    d = usb.core.find(idVendor=VID)
    if d is not None and d.idProduct == PID_CDC:
        return True
    if d is not None:
        try:
            d.reset()
        except Exception:
            pass
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = usb.core.find(idVendor=VID)
        if d is not None and d.idProduct == PID_CDC:
            return True
        time.sleep(0.2)
    return False


def boot_app(timeout=10.0):
    """Send 'b' on the CDC console to load the app from flash; wait for PID 1."""
    dev = usb.core.find(idVendor=VID, idProduct=PID_LINEAR)
    if dev is not None:
        return True
    tty = _find_tty()
    if not tty:
        return False
    fd = os.open(tty, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[3] = 0  # no line discipline translation
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        os.write(fd, b"b")
    finally:
        os.close(fd)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if usb.core.find(idVendor=VID, idProduct=PID_LINEAR) is not None:
            time.sleep(2.5)  # let the app settle before init
            return True
        time.sleep(0.2)
    return False


class LinearATAPort:
    """One FXS port (the dongle has two; events carry the port index)."""

    def __init__(self, device):
        self.dev = device
        self.port = 0
        self.on_hook = False
        self.on_event = None  # callback(kind, value)

    def _emit(self, kind, value):
        if self.on_event:
            self.on_event(kind, value)


class LinearATA:
    def __init__(self, verbose=True):
        self.verbose = verbose
        self.dev = None
        self.last_heart = 0
        self.heart_state = 0
        self.pending = b""
        self.ports = [LinearATAPort(self), LinearATAPort(self)]
        self.ports[1].port = 1

    # ---- lifecycle -------------------------------------------------
    def log(self, *a):
        if self.verbose:
            print(*a, flush=True)

    def open(self):
        if not self.connect():
            raise RuntimeError("dongle not reachable")

    def connect(self, cold=False):
        if cold:
            reset_to_bootloader()
            if not boot_app():
                self.log("cold boot failed")
                return False
        dev = usb.core.find(idVendor=VID, idProduct=PID_LINEAR)
        if dev is None:
            if not reset_to_bootloader() or not boot_app():
                self.log("device not found and could not boot")
                return False
            dev = usb.core.find(idVendor=VID, idProduct=PID_LINEAR)
            if dev is None:
                return False
        try:
            dev.set_configuration()
        except Exception:
            pass
        cfg = dev.get_active_configuration()
        itf = cfg[(0, 0)]
        try:
            usb.util.claim_interface(dev, itf)
        except Exception:
            pass
        self.dev = dev
        return True

    # ---- io --------------------------------------------------------
    def write(self, hexstr, timeout=2000):
        self.dev.write(EP_OUT, bytes.fromhex(hexstr), timeout)

    def read(self, timeout=40, size=2048):
        try:
            return bytes(self.dev.read(EP_IN, size, timeout))
        except usb.core.USBError:
            return None

    def init(self):
        self.log("init: replaying %d frames" % len(OPS))
        for delay_ms, frame in OPS:
            if delay_ms:
                time.sleep(delay_ms / 1000.0)
            try:
                self.write(frame)
            except usb.core.USBError as exc:
                self.log("init write failed at %s: %s" % (frame[:8], exc))
                return False
            reply = self.read(120)
            # 40/60 xx 1d is a readiness poll: repeat until it reports 0x00
            if frame[:6] in ("40031d", "60031d"):
                tries = 0
                while reply and len(reply) >= 3 and reply[2] != 0x00 and tries < 30:
                    time.sleep(0.01)
                    try:
                        self.write(frame)
                    except usb.core.USBError:
                        return False
                    reply = self.read(120)
                    tries += 1
        self.log("init: armed")
        return True

    # ---- main loop -------------------------------------------------
    def _handle_frame(self, data):
        if len(data) < 2:
            return
        t = data[0]
        if t == 0xA5 and len(data) >= 3:
            # frame is [type][len][payload]; data[1] is the LENGTH byte, not a
            # port. Observed hook frames carry no port field (payload[1]=0x00
            # in all captures) — assume the single active port.
            port = 0
            hook = bool(data[2])
            p = self.ports[min(port, 1)]
            p.on_hook = hook
            self.log("EVENT hook port%d %s" % (port + 1, "OFFHOOK" if hook else "ONHOOK"))
            try:
                if hook:
                    self.write(DIAL_TONE)
                else:
                    self.write(TONE_OFF)
            except usb.core.USBError:
                pass
            p._emit("hook", hook)
        elif t == 0xA4 and len(data) >= 3:
            port = 0  # see 0xA5 note above: no port field in captured frames
            digit = chr(data[2]) if 0x20 <= data[2] < 0x7F else "?"
            self.log("EVENT dtmf port%d %r" % (port + 1, digit))
            try:
                self.write(TONE_OFF)  # stop dial tone, as nvox does on first digit
            except usb.core.USBError:
                pass
            self.ports[max(0, min(port, 1))]._emit("dtmf", digit)
        elif t == 0x64:
            self.log("EVENT phys %s" % data.hex())
            for p in self.ports:
                p._emit("phys", data.hex())
        elif t in (0xA0, 0xA1, 0xA2, 0x80, 0x81, 0x82, 0x84):
            return  # command acks / heartbeat replies
        else:
            self.log("EVENT other %s" % data.hex())

    def poll(self, timeout=250):
        data = self.read(timeout)
        if data:
            self.pending += data
            while len(self.pending) >= 2:
                ln = self.pending[1]
                if ln < 2 or ln > len(self.pending):
                    # frames here are type,len,payload; guard against garbage
                    if len(self.pending) >= 4 and self.pending[0] in (0xA4, 0xA5):
                        ln = 4
                    else:
                        self.pending = b""
                        return
                frame, self.pending = self.pending[:ln], self.pending[ln:]
                self._handle_frame(frame)
        now = time.time()
        if now - self.last_heart >= 1.0:
            self.last_heart = now
            self.heart_state ^= 1
            try:
                self.write("a003%02x" % self.heart_state, 500)
            except usb.core.USBError:
                pass

    def run_forever(self):
        self.init()
        self.log("running: pick up the phone")
        while True:
            self.poll()
            time.sleep(0.01)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--cold", action="store_true", help="reset+boot the dongle first")
    args = ap.parse_args()
    ata = LinearATA()
    ata.connect(cold=args.cold) or sys.exit("no dongle")
    try:
        ata.run_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
