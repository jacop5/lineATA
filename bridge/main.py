#!/usr/bin/env python3
"""LineATA bridge daemon: supervisor + CLI.

Wires the pieces together:
  [dongle] --linearusb--> [events] --> [PortFSM] --> [SIP toward the PBX]
                                   -> [ring experiments / voice harness]

Modes:
  run          full daemon: init, poll events, FSM, SIP register
  ring-test    interactive ring experiment harness
  voice-test   voice framing sweep harness
  ping         transport health check (FW date query)
"""
import argparse
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import linearusb as L
import initctl
import events as EV
import ring as RINGMOD
import voice as VOICEMOD
import sipside


def get_dongle(retries=5, verbose=True):
    for i in range(retries):
        if L.cold_boot(verbose=verbose):
            try:
                return L.Linear()
            except Exception as e:
                if verbose:
                    print("open failed:", e)
        if verbose:
            print(f"attach retry {i + 1}/{retries}")
        time.sleep(2)
    raise SystemExit("dongle not reachable")


def usb_health(lin):
    r = lin.cmd(bytes.fromhex("c602"))
    return len(r) >= 2 and r[0] == 0xC6


def poll_once(lin, do_poll=(0x40, 0x60)):
    """One poll cycle; yields decoded events."""
    try:
        for cmd in do_poll:
            # steady-state poll flag is 0x1d (nvox trace); 0x00 crash-stops the FXS app
            lin.w(bytes([cmd, 0x03, 0x1d]))
        while True:
            data = lin.r(1000, 120)
            if not data:
                return
            yield EV.decode(bytes(data))
    except usb.core.USBError as e:
        print("poll transport gone:", e)
        raise


def make_outgoing(sip):
    def outgoing(port_idx, number):
        try:
            call = sip.dial(number)
            print(f"port {port_idx} dialing {number}")
            return call
        except Exception as e:
            print(f"dial failed: {e}")
    return outgoing


def run(args):
    lin = get_dongle()
    print("transport ok:", usb_health(lin))
    n_ok, n_acked = initctl.replay(lin, on_progress=lambda i, n: print(f"init {i}/{n}") if i % 100 == 0 else None)
    print(f"init replay: writes ok={n_ok} acked={n_acked}")

    sip = sipside.SipAgent(host=args.pbx, port=args.pbx_port,
                           user=args.user, password=args.password)
    fsms = [sipside.PortFSM(i, make_outgoing_dummy()) for i in range(2)]
    # (bind real outgoing once the SIP agent is registered: see TODO in README)
    print("event loop up (Ctrl-C to stop)")
    while True:
        for ev in poll_once(lin):
            if isinstance(ev, EV.HookEvent):
                which = "serial" if ev.port == 0 else "parallel"
                print(f"hook port{ev.port}: {'OFFHOOK' if ev.offhook else 'ONHOOK'}")
            elif isinstance(ev, EV.DtmfEvent):
                print(f"DTMF port{ev.port}: {ev.digit}")
            elif isinstance(ev, EV.Unknown):
                print("unknown msg:", hex(ev.cmd), ev.payload.hex()[:40])
        time.sleep(0.09)


def make_outgoing_dummy():
    def outgoing(port_idx, number):
        print(f"[fsm] port {port_idx} wants to call {number} (SIP wiring TODO)")
    return outgoing


def ring_test(args):
    lin = get_dongle()
    port = args.port
    print("make sure a phone is on the fxS port", port)
    for name, results in RINGMOD.try_hypothesis(lin, port).items():
        print(f"--- {name}")
        for op, observed in results:
            print(f"    {op[-10:]} -> {observed}")
    RINGMOD.stop_ring(lin, port)


def voice_test(args):
    lin = get_dongle()
    print("sweeping voice framing hypotheses (dongle may dislike some)")
    for name, res in VOICEMOD.sweep(lin, args.port).items():
        print(f"{name}: {res}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["run", "ring-test", "voice-test"], default="run", nargs="?")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--pbx", default="127.0.0.1")
    ap.add_argument("--pbx-port", type=int, default=5060)
    ap.add_argument("--user", default="101")
    ap.add_argument("--password", default="lineata101")
    args = ap.parse_args()
    if args.mode == "run":
        try:
            run(args)
        except KeyboardInterrupt:
            print("\nbye")
    elif args.mode == "ring-test":
        ring_test(args)
    elif args.mode == "voice-test":
        voice_test(args)


if __name__ == "__main__":
    main()
