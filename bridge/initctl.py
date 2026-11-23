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
from initsequence import OPS as ALL_OPS

# Clean init = ops 0..83 (ends with the RAM-mute markers 4508/65081a04...);
# everything after op 84 in the capture is steady-poll/live-call traffic that
# must NOT be replayed (it live-crashes the app).
INIT_OPS = ALL_OPS[:84]
# Steady heartbeat: the nvox idle loop - poll pairs interleaved with per-port
# sync blocks (ops 84..115 of the capture), repeating forever. Polling alone
# starves the app and it reaps itself after a few seconds.
STEADY_CYCLE = ALL_OPS[84:116]
# Post-init transition: run once after replay(). The sync blocks here are a
# bring-up confirmation, NOT part of the idle loop.
TRANSITION = ALL_OPS[92:116]

RESET_PORT0 = "070303"
RESET_PORT1 = "270303"
STOP_PORTS = "810300"
POST_RESET_SLEEP = 0.15          # let the SLICs come back before repl drive


class InitFailure(Exception):
    pass


def _h(s):
    return bytes.fromhex(s)


def reset_ports(lin):
    """Reset both FXS channels.

    NOTE: `81 03 00` (STOP) is only safe as the very first host op on a
    freshly booted device; on a live session it halt-crashes the app
    (observed 2026-11-23). reset_ports is only called at the start of
    replay(), which is only invoked right after cold_boot().
    """
    lin.cmd(_h(STOP_PORTS))
    time.sleep(0.05)
    lin.cmd(_h(RESET_PORT0))
    lin.cmd(_h(RESET_PORT1))
    time.sleep(POST_RESET_SLEEP)


def run_transition(lin):
    """Execute the post-init transition (sync blocks) once."""
    for delay, hexop in TRANSITION:
        if delay:
            time.sleep(min(delay, 50) / 1000.0)
        lin.w(_h(hexop))
        try:
            lin.r(1000, 8)
        except usb.core.USBError:
            pass


def steady_tick(lin):
    """One bare poll pair (both ports) - the true idle heartbeat.
    Hosts this at ~11ms cadence per pair, like the nvox trace."""
    lin.w(bytes([0x40, 0x03, 0x1d]))
    lin.w(bytes([0x60, 0x03, 0x1d]))
    time.sleep(0.011)


def steady_full_cycle(lin):
    """Legacy night-session config: full 84-115 sync cycle per tick (~85ms).
    Proven stable for 120s+ on 2026-11-23 night session."""
    for delay, hexop in STEADY_CYCLE:
        if delay:
            time.sleep(min(delay, 50) / 1000.0)
        lin.w(_h(hexop))
        try:
            lin.r(1000, 8)
        except usb.core.USBError:
            pass


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


def replay(lin, on_progress=None, deadline_s=12.0, inbox=None):
    """Replay the embedded init ops. Returns (n_ok, n_acked).

    CRITICAL: the dongle firmware has a ~15s init watchdog
    (nvox config fxs.init_timer=15000): the FXS app self-resets if the
    bring-up is not finished in time. Replay must complete well under
    deadline_s (default 12s budget).
    """
    reset_ports(lin)
    t_start = time.time()
    n_ok = n_acked = 0
    for i, (delay, hexop) in enumerate(INIT_OPS):
        if delay:
            # keep pacing but fit the watchdog budget (cap 50ms cá)
            time.sleep(min(delay, 50) / 1000.0)
        try:
            lin.w(_h(hexop))
            n_ok += 1
        except usb.core.USBError as e:
            raise InitFailure(
                f"write failed at op {i} ({hexop[:16]}...): {e}") from e
        # drain EVERY op: the FXS app requires each reply consumed
        # before the next write (strict S->R alternation in all traces)
        try:
            r = lin.r(1000, 30)
            if r:
                n_acked += 1
                if inbox is not None:
                    inbox.append(bytes(r))
        except usb.core.USBError:
            pass
        if on_progress and i % 50 == 0:
            on_progress(i, len(INIT_OPS))
    drain(lin, quiet_ms=60)
    elapsed = time.time() - t_start
    if elapsed > deadline_s:
        raise InitFailure(f"replay too slow: {elapsed:.1f}s (watchdog ~15s)")
    return n_ok, n_acked
