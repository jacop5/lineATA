#!/usr/bin/env python3
"""Linear KN-3110 FXS dongle control - PoC client.

Implements the wire protocol decoded from Keenetic's nvox daemon:
- pre-boot CDC serial mode (VID 31b7 PID 0002)
- booted bulk mode (VID 31b7 PID 0001, EP 0x81 in / 0x02 out,
  bulk via the 'Linear' product interface 0)
- boot trigger: ASCII 'boot\r\n' on the CDC serial console (confirmed).

See PROTOCOL.md for the full decoded spec.
"""
import os, sys, time, argparse, socket, struct, select, termios, fcntl
import usb.core, usb.util

VID = 0x31b7
PID_CDC = 0x0002
PID_LINEAR = 0x0001
EP_IN = 0x81
EP_OUT = 0x02

MAC_TIOCMGET = 0x4004746a
MAC_TIOCMSET = 0x8004746a
TIOCM_DTR, TIOCM_RTS = 0x02, 0x04
TIOCM_CAR, TIOCM_DSR = 0x01, 0x04


def ts():
    return time.strftime("%H:%M:%S")


class SerialPhase:
    """Cold dongle on CDC. Discover the trigger that flips it to Linear mode."""

    def __init__(self, path=None):
        if path is None:
            for p in ("/dev",):
                for f in sorted(os.listdir("/dev")):
                    if f.startswith("cu.usbmodem"):
                        path = "/dev/" + f
                        break
        if not path:
            raise FileNotFoundError("no cu.usbmodem device")
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        a = termios.tcgetattr(self.fd)
        a[0] = 0  # iflag
        a[1] = 0  # oflag
        a[2] = termios.CS8  # cflag baud set separately
        a[3] |= termios.CLOCAL
        a[6][termios.VMIN] = 0
        a[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, a)
        # macOS: speed lives directly in the iflag/oflag c_cc fields
        import array
        a[4] = termios.B115200; a[5] = termios.B115200
        termios.tcsetattr(self.fd, termios.TCSANOW, a)
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def dtr(self, on=True):
        m = struct.pack("I", 0)
        fcntl.ioctl(self.fd, MAC_TIOCMGET, m)
        bits = struct.unpack("I", m)[0]
        if on:
            bits |= TIOCM_DTR | TIOCM_RTS
        else:
            bits &= ~(TIOCM_DTR | TIOCM_RTS)
        fcntl.ioctl(self.fd, MAC_TIOCMSET, struct.pack("I", bits))

    def write(self, data: bytes):
        os.write(self.fd, data)

    def read(self, timeout=1.0) -> bytes:
        out = b""
        end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.2)
            if r:
                out += os.read(self.fd, 4096)
        return out

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def wait_hotplug():
    for i in range(60):
        dev = usb.core.find(idVendor=VID, idProduct=PID_LINEAR)
        if dev:
            return dev
        time.sleep(1)
    return None


class Linear:
    def __init__(self):
        self.dev = None

    def open(self):
        self.dev = usb.core.find(idVendor=VID, idProduct=PID_LINEAR)
        if not self.dev:
            raise RuntimeError("Linear not found (booted state)")
        try:
            self.dev.set_configuration()
        except usb.core.USBError:
            pass
        cfg = self.dev.get_active_configuration()
        itf = cfg[(0, 0)]
        usb.util.claim_interface(self.dev, itf)
        return itf

    def cmd(self, data: bytes, rlen=512, timeout=500, expect_reply=True):
        self.dev.write(EP_OUT, data, timeout)
        if not expect_reply:
            return b""
        return bytes(self.dev.read(EP_IN, rlen, timeout))


def stage_boot():
    print(f"[{ts()}] CDC serial phase on", end=" ")
    sp = SerialPhase()
    print(sp.path)
    triggers = [
        ("DTR|RTS raise", lambda: sp.dtr(True)),
        ("FXS START\\r\\n", lambda: sp.write(b"FXS START\r\n")),
        ("FXS RESET\\r\\n", lambda: sp.write(b"FXS RESET\r\n")),
        ("binary 0x81 03 00 (START)", lambda: sp.write(bytes.fromhex("810300"))),
        ("binary 0x80 03 01 (INIT)", lambda: sp.write(bytes.fromhex("800301"))),
        ("binary 0xa0 03 01", lambda: sp.write(bytes.fromhex("a00301"))),
        ("FXS GET FW VERSION\\r\\n", lambda: sp.write(b"FXS GET FW VERSION\r\n")),
    ]
    for name, act in triggers:
        try:
            act()
        except OSError as e:
            print(f"  [{name}] io error: {e}")
        resp = sp.read(1.0)
        pid = usb.core.find(idVendor=VID) or None
        pidn = pid.idProduct if pid else None
        print(f"  [{name}] resp={resp.hex()} pid={hex(pidn) if pidn else None}")
        if pidn == PID_LINEAR:
            print(f"[{ts()}] *** BOOT TRIGGER FOUND: {name}")
            sp.close()
            return True
        time.sleep(0.5)
    sp.close()
    return False


def stage_operate():
    lin = Linear()
    itf = lin.open()
    print(f"[{ts()}] claimed {itf}")
    for name, frame in [
        ("GET FW DATE", "c602"),
        ("GET FW VERSION", "c102"),
        ("IDENT", "c202"),
    ]:
        r = lin.cmd(bytes.fromhex(frame))
        print(f"  {name}: {r.hex()} | {r[2:].decode(errors='replace') if len(r) > 2 else ''}")
    print(f"[{ts()}] polling hooks (Ctrl-C to stop) for port 0/1")
    seq = 0
    pending = {}
    while True:
        seq += 1
        for port, cmd in ((0x40, "400300"), (0x60, "400300")):
            b = bytes.fromhex(cmd)
            lin.dev.write(EP_OUT, b, 500)
        # read once per poll window
        time.sleep(0.05)
        try:
            data = lin.dev.read(EP_IN, 1000, 200)
        except usb.core.USBError:
            continue
        if data:
            d = bytes(data)
            if d[0] in (0xa4, 0xa5):
                if d[0] == 0xa5:
                    state = "OFFHOOK" if d[2] else "ONHOOK"
                    print(f"[{ts()}] port hook: {state}")
                else:
                    print(f"[{ts()}] DTMF {chr(d[2])}")
            elif d[0] in (0x40, 0x60):
                pass
            else:
                print(f"[{ts()}] msg {d.hex()[:60]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="auto", choices=["auto", "boot", "operate", "info"])
    args = ap.parse_args()
    have = usb.core.find(idVendor=VID, idProduct=PID_LINEAR) or usb.core.find(idVendor=VID, idProduct=PID_CDC)
    if not have:
        print(f"[{ts()}] no Linear present")
        return 1
    print(f"[{ts()}] device PID {hex(have.idProduct)}")
    if args.cmd == "auto":
        if have.idProduct == PID_LINEAR:
            return stage_operate() or 0
        ok = stage_boot()
        if ok:
            time.sleep(1)
            return stage_operate() or 0
        return 1
    if args.cmd == "boot":
        return 0 if stage_boot() else 1
    if args.cmd == "operate":
        stage_operate()
    if args.cmd == "info":
        lin = Linear(); lin.open()
        for name, frame in [("FW DATE","c602"),("FW VER","c102"),("IDENT","c202")]:
            r = lin.cmd(bytes.fromhex(frame))
            print(name, r.hex())


if __name__ == "__main__":
    sys.exit(main() or 0)
