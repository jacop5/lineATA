#!/usr/bin/env python3
"""Analyze a golden-capture shim log (usbsnoop4 format) from the Keenetic router.

Input: timestamped lines from /tmp/usbsnoop.log written by the LD_PRELOAD shim:
    [ts] S <ep> <n> <hex>              SUBMITURB (OUT bulk if ep=2, IN poll if ep=129)
    [ts] R <ep> <n> [<hex>] st=<n>     REAPURB
    [ts] O <path>                      open()
    [ts] w <fd> <n> <hex>              serial write
    [ts] r <fd> <n> <hex>              serial read

Output: structured timeline + comparison blocks:
  - CDC/serial phase ops (open, writes, reads -> boot dance)
  - bulk phase ops in order with inter-op delays
  - steady-state detection (poll runs)
  - device event frames (a4/a5/64/...) with host response latency
"""
import sys, re, collections

LINE = re.compile(r"^\[(\d+)\] ([SROwTrT]) (\S+)(?: (\d+))?(.*)$")


def parse(path):
    evs = []
    for ln in open(path, errors="replace"):
        m = re.match(r"^\[(\d+)\] (\S+) (.*)$", ln.strip())
        if not m:
            continue
        t = int(m.group(1))
        tag = m.group(2)
        rest = m.group(3).strip()
        evs.append((t, tag, rest))
    return evs


def analyze(path):
    evs = parse(path)
    t0 = evs[0][0] if evs else 0
    print(f"events: {len(evs)}")
    kinds = collections.Counter(t for _, t, _ in evs)
    print("by kind:", dict(kinds))
    # serial phase
    serial = [e for e in evs if e[1] in ("O", "w", "r", "T")]
    bulk_w = [e for e in evs if e[1] == "S" and e[2].split()[0] == "2"]
    bulk_r = [e for e in evs if e[1] == "R" and e[2].split()[0] == "129"
              and len(e[2].split()) > 1 and not e[2].split()[2].startswith("st=")]
    print(f"serial ops: {len(serial)}, bulk OUT: {len(bulk_w)}, bulk IN data: {len(bulk_r)}")
    print("\n=== first 20 ops (context) ===")
    shown = 0
    for t, tag, rest in evs:
        print(f"+{(t-t0)/1000:8.3f}s {tag} {rest[:70]}")
        shown += 1
        if shown >= 20:
            break
    print("\n=== serial detail (O/w/r) ===")
    for t, tag, rest in serial[:40]:
        print(f"+{(t-t0)/1000:8.3f}s {tag} {rest[:70]}")
    print("\n=== device events + host response ===")
    for i, (t, tag, rest) in enumerate(evs):
        if tag == "R" and len(rest.split()) > 1:
            try:
                d = bytes.fromhex(rest.split()[1])
            except (ValueError, IndexError):
                continue
            if d and d[0] in (0xA4, 0xA5, 0x64, 0x74, 0x75):
                print(f"+{(t-t0)/1000:8.3f}s DEV {d.hex()}")
                for nt, ntag, nrest in evs[i+1:i+4]:
                    if ntag in ("S", "w"):
                        print(f"             -> host {ntag}: {nrest[:44]}")
                        break


if __name__ == "__main__":
    analyze(sys.argv[1])
