#!/usr/bin/env python3
"""Init supervision for the Linear dongle: reset + replay + health guard.

The capture-derived init (initsequence.OPS) is what nvox 0.8.2.96 brings up
a fresh FXS with: per-port start/stop handoff, poll-driven state walk
(flags 0x2f -> 0x03 -> 0x0d -> 0x1d), interrupt setup, codec coefficient
uploads, register init batches, activation.

Replay rules learned the hard way (pipe error on blind replay):
- always STOP and RESET both ports first (`81 03 00`, `07/27 03 03`),
- pace frames by the captured inter-frame delays (capped),
- drain replies after every write; abort on pipe error and report.
"""
import time
import usb.core

import initsequence

RESET_PORT0 = "070303"
RESET_PORT1 = "270303"
STOP_PORTS = "810300"
POST_RESET_SLEEP = 0.15          # let the SLICs come back before repl drive


class InitFailure(Exception):
    pass


def _h(s):
    return bytes.fromhex(s)


def reset_ports(lin):
    """Stop activity and soft-reset both FXS channels (fresh SLIC state)."""
    lin.cmd(_h(STOP_PORTS))
    time.sleep(0.05)
    lin.cmd(_h(RESET_PORT0))
    lin.cmd(_h(RESET_PORT1))
    time.sleep(POST_RESET_SLEEP)


def drain(lin, quiet_ms=120):
    """Consume outstanding replies; returns count of unexpected failures."""
    bad = 0
    end = time.time() + quiet_ms / 1000.0
    while time.time() < end:
        try:
            data = lin.r(1000, 50)
        except usb.core.USBError:
            bad += 1
            time.sleep(0.05)
            continue
        if not data:
            break
    return bad


def replay(lin, speed=1.0, on_progress=None):
    """Replay the embedded init ops. Returns (n_ok, n_acked)."""
    reset_ports(lin)
    n_ok = n_acked = 0
    for i, (delay, hexop) in enumerate(initsequence.OPS):
        if delay:
            time.sleep(min(delay, 3000) / 1000.0 * speed)
        try:
            lin.w(_h(hexop))
            n_ok += 1
        except usb.core.USBError as e:
            raise InitFailure(
                f"write failed at op {i} ({hexop[:16]}...): {e}") from e
        # short drain: the capture interleaves reads; we tolerate absence
        try:
            r = lin.r(1000, 30)
            if r:
                n_acked += 1
        except usb.core.USBError:
            pass
        if on_progress and i % 25 == 0:
            on_progress(i, len(initsequence.OPS))
    drain(lin)
    return n_ok, n_acked
