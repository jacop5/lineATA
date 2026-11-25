#!/usr/bin/env python3
"""Bounded live call session with USB/network evidence and control-only logs.

Run from an isolated flat deployment. Reads the SIP secret via a hidden prompt
or existing remote oracle config; never copies it or uses process arguments.
This harness accepts real incoming calls and can physically ring the phone.
"""
import argparse
import getpass
from pathlib import Path
import signal
import subprocess
import threading
import time

from ata import LinearATA, HANGUP, reset_to_bootloader


class LoggedDevice:
    def __init__(self, dev, log):
        self.dev = dev
        self.log = log

    def __getattr__(self, key):
        return getattr(self.dev, key)

    def write(self, ep, data, timeout):
        if data[0]:
            self.log('USB TX', data.hex())
        return self.dev.write(ep, data, timeout)


class LoggedATA(LinearATA):
    closing = False
    enable_candidate = False
    candidate_call = None
    candidate_due = 0

    def connect(self):
        if not super().connect():
            return False
        self.dev = LoggedDevice(self.dev, self.log)
        return True

    def handle_frame(self, frame):
        if frame[0]:
            self.log('USB RX', frame.hex())
        super().handle_frame(frame)

    def handle_sip_message(self, text, addr):
        self.log('SIP RX', addr, text.split('\r\n', 1)[0])
        super().handle_sip_message(text, addr)

    def on_invite(self, text, addr):
        if self.closing:
            self.send_response(text, addr, 480, 'Temporarily Unavailable')
        else:
            super().on_invite(text, addr)

    def answer_incoming(self):
        super().answer_incoming()
        if self.enable_candidate and self.call and self.call.media:
            self.candidate_call = self.call
            self.candidate_due = time.monotonic() + .2

    def drain_commands(self):
        super().drain_commands()
        if self.candidate_due and time.monotonic() >= self.candidate_due:
            call = self.candidate_call
            self.candidate_due = 0
            self.log('EXPERIMENT candidate 270300 after answer')
            self.write('270300', guard=lambda: self.call is call and not call.closed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--oracle-config')
    parser.add_argument('--pbx', required=True)
    parser.add_argument('--local-ip', required=True)
    parser.add_argument('--login', required=True)
    parser.add_argument('--seconds', type=float, default=240)
    parser.add_argument('--enable-candidate', action='store_true',
                        help='test oracle 270300 command 200 ms after incoming answer')
    args = parser.parse_args()
    if args.oracle_config:
        values = {}
        for line in Path(args.oracle_config).read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip()
        if values.get('sip.lines[0].login') != args.login:
            raise SystemExit('oracle config login does not match requested test extension')
        password = values['sip.lines[0].password']
    else:
        password = getpass.getpass('Extension secret: ')
    ata = LoggedATA(dict(pbx_ip=args.pbx, pbx_port=5060, domain=args.pbx,
                         local_ip=args.local_ip, login=args.login,
                         password=password, sip_port=5062,
                         dial_timeout=3, cold=True))
    ata.enable_candidate = args.enable_candidate
    requested_stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: requested_stop.set())
    signal.signal(signal.SIGINT, lambda *_: requested_stop.set())
    captures = []
    failed = False
    try:
        for name, interface, filters in (
            ('usb', 'usbmon1', []),
            ('network', 'any', ['host', args.pbx, 'and', 'udp']),
        ):
            output = Path('session-' + name + '.pcap')
            if output.exists():
                raise RuntimeError('refusing to overwrite an existing capture')
            log = open('session-' + name + '.tcpdump.log', 'w')
            process = subprocess.Popen(['tcpdump', '-i', interface, '-s', '0', '-U',
                                        '-w', str(output), *filters], stdout=log, stderr=log)
            captures.append((process, log))
        time.sleep(.3)
        if any(process.poll() is not None for process, _ in captures):
            raise RuntimeError('capture process failed to start')
        if not reset_to_bootloader() or not ata.run():
            raise RuntimeError('native initialization failed')
        ata.start_sip()
        ata.log('SESSION READY: extension', args.login, 'duration', args.seconds)
        deadline = time.monotonic() + args.seconds
        while not ata.stop.is_set() and not requested_stop.is_set() and time.monotonic() < deadline:
            requested_stop.wait(.25)
        failed = ata.stop.is_set()
    finally:
        ata.closing = True
        if ata.dev is not None and not ata.stop.is_set():
            call = ata.call
            ata.stop_ringing()
            if call:
                call.hangup()
                if call.established:
                    for frame in HANGUP:
                        ata.write(frame)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                if not ata.usb_commands.pending and ata.usb_commands.commands.empty():
                    break
                time.sleep(.01)
        if hasattr(ata, 'sip_sock'):
            for _ in range(2):
                ata.reg_event.clear()
                ata.reg_ok = False
                request = ata.registration_request(0).encode()
                ata.sip_sock.sendto(request, (args.pbx, 5060))
                ata.reg_event.wait(1)
                if ata.reg_ok:
                    ata.log('SESSION contact removed')
                    break
            ata.sip_sock.close()
        ata.stop.set()
        for process, log in captures:
            process.terminate()
            process.wait(timeout=5)
            log.close()
        ata.log('SESSION stopped', 'usb_in', ata.stat_in, 'usb_out', ata.stat_out)
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
