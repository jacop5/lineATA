#!/usr/bin/env python3
"""Linear USB transport + capture replay driver.

Construction from PROTOCOL.md and captures/capture-call2.log (full init of
the KN-3110 by nvox 0.8.2.96, timestamped).

Modes:
- boot:      cold CDC -> 'boot\\r\\n' -> wait re-enumeration to 31b7:0001
- info:      query FW date/version/identity
- init:      replay the post-boot ProSLIC init from the capture, then idle
- poll:      run the idle event loop, print hook/DTMF events
"""
import os, sys, time, glob, struct, termios, select, argparse
import usb.core, usb.util

VID = 0x31b7
PID_CDC = 0x0002
PID_LINEAR = 0x0001
EP_IN = 0x81
EP_OUT = 0x02

CAPTURE_FILE = os.path.join(os.path.dirname(__file__), "..", "captures", "capture-call2.log")


# ---------------------------------------------------------------- CDC phase

def find_serial():
    for f in sorted(glob.glob("/dev/cu.usbmodem*")):
        return f
    return None


def cold_boot(verbose=True):
    """Cold CDC -> 'boot' -> Linear mode. Returns True when PID 1 present."""
    d = usb.core.find(idVendor=VID, idProduct=PID_LINEAR)
    if d is not None:
        if verbose:
            print("already in Linear mode")
        return True
    path = find_serial()
    if not path:
        print("no CDC serial node")
        return False
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    a = termios.tcgetattr(fd)
    a[3] |= termios.CLOCAL
    a[4] = termios.B115200
    a[5] = termios.B115200
    termios.tcsetattr(fd, termios.TCSANOW, a)
    try:
        os.write(fd, b"boot\r\n")
    except OSError:
        pass
    time.sleep(0.2)
    os.close(fd)
    for _ in range(20):
        d = usb.core.find(idVendor=VID, idProduct=PID_LINEAR)
        if d is not None:
            if verbose:
                print("booted -> Linear mode")
            return True
        time.sleep(0.5)
    print("boot failed")
    return False


# ---------------------------------------------------------------- transport

class Linear:
    def __init__(self):
        self.dev = usb.core.find(idVendor=VID, idProduct=PID_LINEAR)
        if self.dev is None:
            raise RuntimeError("Linear not present (run cold_boot first)")
        try:
            self.dev.set_configuration()
        except usb.core.USBError:
            pass
        cfg = self.dev.get_active_configuration()
        self.itf = cfg[(0, 0)]
        usb.util.claim_interface(self.dev, self.itf)

    def w(self, data: bytes):
        return self.dev.write(EP_OUT, data, 2000)

    def r(self, n=1000, timeout=300):
        try:
            return bytes(self.dev.read(EP_IN, n, timeout))
        except usb.core.USBError as e:
            if e.errno == 110:  # timeout
                return b""
            raise

    def cmd(self, data: bytes, timeout=300):
        self.w(data)
        return self.r(1000, timeout)

    def close(self):
        try:
            usb.util.release_interface(self.dev, self.itf)
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass


# ---------------------------------------------------------------- replay

def load_capture(path=CAPTURE_FILE):
    """Return ordered (t, tag, ep, n, hex) pairs from a shim capture."""
    evs = []
    with open(path) as f:
        for ln in f:
            p = ln.split()
            if len(p) < 4 or not ln.startswith("["):
                continue
            t = int(p[0][1:-1])
            if p[1] == "S" and p[2] == "2":
                hx = "".join(p[4:])
                try:
                    evs.append((t, "S", bytes.fromhex(hx)))
                except ValueError:
                    pass
            elif p[1] == "R" and p[2] == "129":
                payload = p[4] if len(p) > 5 else ""
                try:
                    evs.append((t, "R", bytes.fromhex(payload) if payload else b""))
                except ValueError:
                    pass
    return evs


def init_boundary(evs):
    """First bulk frame after the last full enumerations = init start."""
    idx = None
    for i, (t, tag, d) in enumerate(evs):
        if tag == "S" and d[:2] == bytes([0x80, 0x03]):
            idx = i
    return idx  # last '80 03 01' begins the real init


def replay_init(lin, verbose=True, speedup=1.0):
    evs = load_capture()
    start = init_boundary(evs)
    if start is None:
        raise RuntimeError("init start not found in capture")
    t0, _, _ = evs[start]
    prev = t0
    n_in_bad = 0
    for t, tag, d in evs[start:]:
        dt = (t - prev) / 1000.0
        prev = t
        if dt > 0 and speedup < 1.0:
            time.sleep(min(dt, 0.5) * (1.0 if speedup is None else 1.0) * speedup)
        if tag == "S":
            lin.w(d)
            evs_ack = lin.r(1000, 100)
            if not evs_ack:
                n_in_bad += 1
        # R-tag frames in the capture are validations; we read opportunistically
    return n_in_bad


def cmd_name(d: bytes) -> str:
    base = {0x80: "INIT-A", 0x81: "INIT-START", 0x82: "INIT-CONFIG", 0xa0: "TONE-GATE",
            0xc0: "SET COUNTRY", 0xc1: "GET FW VER", 0xc2: "IDENT", 0xc6: "GET FW DATE",
            0x43: "IRQ-SETUP", 0x4c: "STATUSBLOCK-RD", 0x4d: "STATUSBLOCK-WR"}
    b = base.get(d[0] & ~0x20 if d[0] else 0)
    p = base.get(d[0]) or base.get(d[0] - 0x20)
    return p or f"cmd{d[0]:02x}"


# ---------------------------------------------------------------- event loop

def event_loop(lin, duration=None):
    t_end = time.time() + duration if duration else None
    while True:
        if t_end and time.time() > t_end:
            return
        try:
            lin.w(bytes([0x40, 0x03, 0x00]))
            lin.w(bytes([0x60, 0x03, 0x00]))
        except usb.core.USBError as e:
            print("poll write error:", e)
            return
        data = lin.r(1000, 200)
        if data:
            d = bytes(data)
            if d[0] == 0xa5:
                state = "OFFHOOK" if d[2] else "ONHOOK"
                print(f"[{time.strftime('%H:%M:%S')}] port hook: {state}")
            elif d[0] == 0xa4:
                ch = chr(d[2])
                print(f"[{time.strftime('%H:%M:%S')}] DTMF {ch}")
            elif d[0] in (0x64, 0x65):
                print(f"[{time.strftime('%H:%M:%S')}] phys msg {d.hex()}")
            elif d[0] in (0x40, 0x60):
                st = d[2] if len(d) > 2 else -1
                if st not in (0x00,):  # idle
                    pass
        time.sleep(0.09)  # ~10 Hz like nvox


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["boot", "info", "init", "poll", "all"], default="all", nargs="?")
    ap.add_argument("--speed", type=float, default=0.25)
    ap.add_argument("--poll-seconds", type=int, default=3600)
    args = ap.parse_args()

    def info(lin):
        for name, hexcmd in [("FW DATE", "c602"), ("FW VER", "c102"), ("IDENT", "c202")]:
            r = lin.cmd(bytes.fromhex(hexcmd))
            extra = r[2:].split(b"\x00")[0].decode(errors="replace") if len(r) > 2 else ""
            print(f"{name}: {extra}")

    if args.mode in ("boot", "all"):
        if not cold_boot():
            return 1
    lin = Linear()
    if args.mode in ("info", "all"):
        info(lin)
    if args.mode in ("init", "all"):
        bad = replay_init(lin, speedup=args.speed)
        print("init replay done, ack misses:", bad)
    if args.mode in ("poll", "all"):
        print("polling (Ctrl-C to stop)...")
        event_loop(lin, args.poll_seconds)
    lin.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
