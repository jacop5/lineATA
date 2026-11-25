#!/usr/bin/env python3
"""Parse a usbmon pcap (LINKTYPE_USB_LINUX_MMAPPED=220) into readable
submit/complete records with endpoint, direction, status and data bytes.

Usage: usbmon_parse.py capture.pcap [--dev 65] [--ep 81] [--hex]
"""
import sys, struct, argparse

HDR = struct.Struct("<QBBBBHBBqiiII8sIIII")  # 64 bytes

XFER = {0: "iso", 1: "int", 2: "ctl", 3: "bulk"}
TYPE = {0x53: "S", 0x43: "C", 0x45: "E"}


def read_pcap(path):
    with open(path, "rb") as f:
        gh = f.read(24)
        if len(gh) < 24:
            raise SystemExit("short pcap")
        magic = gh[:4]
        if magic == b"\xd4\xc3\xb2\xa1":
            endian = "<"
        elif magic == b"\xa1\xb2\xc3\xd4":
            endian = ">"
        elif magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d"):
            raise SystemExit("nanosecond pcap unsupported; use tcpdump default")
        else:
            raise SystemExit(f"bad pcap magic {magic!r}")
        major, minor, _, _, snaplen, linktype = struct.unpack(endian + "HHiIII", gh[4:])
        if (major, minor) != (2, 4) or linktype & 0xffff != 220:
            raise ValueError("expected pcap 2.4 with LINKTYPE_USB_LINUX_MMAPPED (220)")
        while True:
            ph = f.read(16)
            if not ph:
                return
            if len(ph) != 16:
                raise ValueError("truncated pcap record header")
            ts_s, ts_u, incl, orig = struct.unpack(endian + "IIII", ph)
            if ts_u >= 1000000 or incl > snaplen or incl > orig:
                raise ValueError("invalid pcap record metadata")
            data = f.read(incl)
            if len(data) < incl:
                raise ValueError("truncated pcap record data")
            if incl != orig:
                raise ValueError("snaplen truncated a USB record; capture with -s 0")
            yield ts_s + ts_u / 1e6, data


def parse_record(data):
    if len(data) < HDR.size:
        raise ValueError('truncated usbmon header')
    (uid, typ, xfer, ep, dev, bus, flag_setup, flag_data,
     ts_sec, ts_usec, status, length, len_cap, setup,
     interval, start_frame, xfer_flags, ndesc) = HDR.unpack_from(data, 0)
    payload = data[HDR.size:]
    if len_cap > len(payload):
        raise ValueError("truncated usbmon payload")
    return dict(
        uid=uid, type=typ, xfer=xfer, ep=ep, dev=dev, bus=bus,
        status=status, length=length, payload=payload[:len_cap],
        setup=setup, flag_data=flag_data, flag_setup=flag_setup,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pcap")
    ap.add_argument("--dev", type=int, default=None, help="filter device number")
    ap.add_argument("--ep", type=lambda s: int(s, 16), default=None, help="filter endpoint (hex, e.g. 81)")
    ap.add_argument("--hex", action="store_true", help="print all payloads as hex")
    args = ap.parse_args()

    t0 = None
    for ts, rec in read_pcap(args.pcap):
        r = parse_record(rec)
        if not r:
            continue
        if args.dev is not None and r["dev"] != args.dev:
            continue
        if args.ep is not None and r["ep"] != args.ep:
            continue
        if t0 is None:
            t0 = ts
        dt = ts - t0
        dirn = "IN " if r["ep"] & 0x80 else "OUT"
        ep = r["ep"] & 0x7F
        xf = XFER.get(r["xfer"], "?")
        # OUT data lives in the submit record; IN data in the complete record
        show_data = (r["type"] == 0x53 and not (r["ep"] & 0x80)) or (r["type"] == 0x43 and (r["ep"] & 0x80))
        if show_data and r["payload"]:
            print(f"{dt:8.3f} {TYPE.get(r['type'],'?')} {dirn} ep{ep} {xf} dev{r['dev']} st={r['status']:4d} len={len(r['payload']):4d} {r['payload'].hex()}")
        elif args.hex and r["payload"]:
            print(f"{dt:8.3f} {TYPE.get(r['type'],'?')} {dirn} ep{ep} {xf} dev{r['dev']} st={r['status']:4d} len={len(r['payload']):4d} {r['payload'].hex()}")
        else:
            print(f"{dt:8.3f} {TYPE.get(r['type'],'?')} {dirn} ep{ep} {xf} dev{r['dev']} st={r['status']:4d} len={r['length']}")


if __name__ == "__main__":
    main()
