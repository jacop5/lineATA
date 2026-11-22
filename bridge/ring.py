#!/usr/bin/env python3
"""Ring generation experiments for the Linear FXS ports.

No real incoming-call capture exists yet (see PROTOCOL.md TODO), so this is
an experiment harness: ordered hypotheses about which wire ops start/stop
ringing on a port, each tried against live hardware with clear output.

Known anchors from the nvox captures / Si32287 knowledge:
- nvox logs "PORT %d RING" / "PORT %d SILENT" in Si32287Preset paths
- cadence config arrives as phone.cadences[N].periods[K] (active/passive ms)
- ProSLIC ring control is register-based; nvox used cmd 0x41/0x61 writes and
  0x4e/0x6e batches; ring-enable writes are candidates in the 0x1a batch.

Every hypothesis is executed with 0x40/0x60 polls between steps to observe
status transitions, and an off-hook check: if the port reports OFFHOOK we
assume the phone rang (someone/answer machine), but the human on a test
phone is the real oracle.
"""
import time

# Candidate frames to try, cheapest/most likely first. Each is a list of
# (hex_frame, pause_seconds). Names explain the reasoning source.
HYPOTHESES = [
    # H1: ProSLIC ring enable via per-port register write (chan=Ram/reg):
    # the init batch "4e1a... 01100904000508000100..." holds linefeed+ring
    # regs; mirror the observed pattern "41 04 <reg16> <val16>" style used
    # for linefeed ops, e.g. rcfg reg 0x1b (ring config in ProSLIC).
    ("reg-ring-bit", [
        ("41040206", 0.1),   # write port0 reg 0x0202 (linefeed) - startup did this
        ("41041b0b", 1.0),    # candidate: ring timer reg first guess
    ]),
]


def try_hypothesis(lin, port, cadence=None, on_event=None):
    """Run ring hypotheses on port; returns dict of results."""
    base = 0x40 if port == 0 else 0x60
    results = {}
    for name, steps in HYPOTHESES:
        outs = []
        for hexop, pause in steps:
            b = bytes.fromhex(hexop)
            if port == 1:
                b = bytes([b[0] | 0x20]) + b[1:]
            try:
                lin.w(b)
            except Exception as e:
                outs.append((hexop, f"ERR {e}"))
                break
            time.sleep(pause)
            try:
                r = lin.r(1000, 200)
                outs.append((hexop, r.hex() if r else "(silent)"))
            except Exception as e:
                outs.append((hexop, f"ERR {e}"))
        results[name] = outs
        if on_event:
            on_event(name, outs)
    return results


def stop_ring(lin, port):
    """Best-effort ring stop: write linefeed reg 0x0202 idle value observed
    at startup ("41 04 02 06" saw value 0xb000 in a read)."""
    b = bytes.fromhex("41040206") if port == 0 else bytes.fromhex("61040206")
    try:
        lin.w(b)
    except Exception:
        pass


# Cadence table ported from nvox.conf (phone.cadences, ms active/passive).
# Index 0 is the IT standard double ring used on the reference site.
CADENCES = [
    [(400, 2000), (400, 2000)],           # cadence[1]-like double ring
    [(1000, 4000)],                       # simple long ring
    [(400, 500)],
]
