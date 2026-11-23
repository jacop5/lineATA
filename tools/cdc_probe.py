#!/usr/bin/env python3
"""CDC probe-replay experiment: replicate the cdc_acm/host probe sequence
into the Keenetic Linear's CDC-console state and watch for a service-mode
self-boot (PID 0x0002 -> 0x0001) without the 'boot' console command.

Candidate sequences derived from Linux cdc_acm driver behavior.
Run while the dongle is freshly attached in CDC state (31b7:0002).
"""
import sys, time
import usb.core, usb.util

VID = 0x31B7
PID_CDC = 0x0002

# standard line coding: 115200 8N1: dwDTERate, bCharFormat(0=1stop),
# bParityType(0=none), bDataBits(8)
LINE_CODING_115200 = bytes([0x00, 0xC2, 0x01, 0x00, 0x00, 0x08])


def dev():
    return usb.core.find(idVendor=VID, idProduct=PID_CDC)


def pid_now():
    d = usb.core.find(idVendor=VID)
    return d.idProduct if d else None


def ctl(d, bm, b, wval, widx, data=b"", timeout=1000, tag=""):
    try:
        r = d.ctrl_transfer(bm, b, wval, widx, data, timeout)
        print(f"  {tag}: ok ({len(r)}B) {bytes(r).hex() if r else ''}")
        return True
    except Exception as e:
        print(f"  {tag}: ERR {e}")
        return False


def watch_for_boot(seconds=20, label=""):
    print(f"  watching {seconds}s for self-boot {label}")
    t0 = time.time()
    while time.time() - t0 < seconds:
        p = pid_now()
        if p == 1:
            print(f"*** LINEAR SELF-BOOT at +{time.time()-t0:.1f}s {label} ***")
            return True
        time.sleep(0.5)
    return False


def main():
    d = dev()
    if not d:
        print("no CDC device - replug the dongle first")
        return 1
    IFACE = 0  # CDC comm/control interface

    seqs = [
        ("A: SET_CONTROL_LINE_STATE(0)",
         lambda: ctl(d, 0x21, 0x22, 0, IFACE, b"", tag="SCLS(0)")),
        ("B: SET_CONTROL_LINE_STATE(3)",
         lambda: ctl(d, 0x21, 0x22, 3, IFACE, b"", tag="SCLS(3)")),
        ("C: SET_LINE_CODING(115200 8N1)",
         lambda: ctl(d, 0x21, 0x20, 0, IFACE, LINE_CODING_115200, tag="SLC")),
        ("D: GET_LINE_CODING",
         lambda: ctl(d, 0xA1, 0x21, 0, IFACE, 7, tag="GLC")),
        ("E: interrupt ep 0x82 short read",
         lambda: intr_read(d)),
        ("F: full cdc_acm open: SCLS(3)+SLC+GLC",
         lambda: (ctl(d, 0x21, 0x22, 3, IFACE, b"", tag="F: SCLS(3)"),
                  ctl(d, 0x21, 0x20, 0, IFACE, LINE_CODING_115200, tag="F: SLC"),
                  ctl(d, 0xA1, 0x21, 0, IFACE, 7, tag="F: GLC"))),
    ]
    for name, fn in seqs:
        print(f"\n=== {name} ===")
        fn()
        if watch_for_boot(15, label=name):
            return 0
    print("\nno self-boot from any probe candidate")
    return 2


def intr_read(d):
    """Probe the CDC notification endpoint (0x82) like a bound cdc_acm."""
    try:
        iface = d[0][(0, 0)]
        usb.util.claim_interface(d, iface)
        data = d.read(0x82, 64, 500)
        print(f"  intr 0x82: {bytes(data).hex()}")
    except Exception as e:
        import usb.util
        try:
            usb.util.release_interface(d, d[0][(0, 0)])
        except Exception:
            pass
        print(f"  intr 0x82: {type(e).__name__} {e}")


if __name__ == "__main__":
    sys.exit(main())
