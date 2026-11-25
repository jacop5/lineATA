#!/usr/bin/env python3
"""Bounded, single-reader USB experiment. Run from an isolated deployment.

No SIP credentials are needed. Capture usbmon externally before running.
Baseline first; --ring adds observed preparation and one ring pulse.
"""
import argparse
import time
import usb.core
from ata import LinearATA, CALL_ANNOUNCE, RING_POLL, RING_ON, RING_OFF, reset_to_bootloader


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ring', action='store_true')
    parser.add_argument('--seconds', type=float, default=15)
    args = parser.parse_args()
    if not reset_to_bootloader():
        raise RuntimeError('could not establish bootloader starting state')
    ata = LinearATA({'cold': True})
    if not ata.connect() or not ata.usb_init():
        raise RuntimeError('initialization failed')
    origin = time.monotonic()
    next_heart = origin
    heart = 0

    def receive(timeout=5):
        frames = ata.decoder.feed(ata.read(timeout) or b'')
        for frame in frames:
            # Record event/control bytes, never audio contents.
            print(f'{time.monotonic()-origin:.6f} IN {frame.hex() if frame[0] else "voice len="+str(len(frame))}', flush=True)
        return frames

    def send(frame):
        print(f'{time.monotonic()-origin:.6f} OUT {frame}', flush=True)
        ata.write(frame)

    def transact(frame):
        send(frame)
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            if any(f[0] == int(frame[:2], 16) for f in receive(20)):
                return
        raise RuntimeError('missing response to ' + frame)

    def pump(seconds):
        nonlocal next_heart, heart
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if time.monotonic() >= next_heart:
                heart ^= 1
                transact('a0030' + str(heart))
                next_heart = time.monotonic() + 1
            receive()

    try:
        pump(args.seconds)
        if args.ring:
            print('STAGE preparation', flush=True)
            for frame in CALL_ANNOUNCE:
                transact(frame)
            pump(2)
            print('STAGE ring-on', flush=True)
            for poll, edge in zip(RING_POLL, RING_ON):
                transact(poll)
                transact(edge)
            pump(0.4)
            print('STAGE ring-off', flush=True)
            for poll, edge in zip(RING_POLL, RING_OFF):
                transact(poll)
                transact(edge)
            pump(10)
        print('RESULT survived; physical ringing requires operator confirmation', flush=True)
    finally:
        usb.util.dispose_resources(ata.dev)


if __name__ == '__main__':
    main()
