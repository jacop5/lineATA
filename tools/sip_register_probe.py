#!/usr/bin/env python3
"""Bounded registration/deregistration check. Never opens USB or places calls.

Uses the daemon's request builder, digest code and SIP listener. Any incoming
INVITE receives 480 without invoking FXS control. Password is read via getpass.
"""
import argparse
import getpass
from pathlib import Path
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.ata import LinearATA


class SilentProbe(LinearATA):
    def on_invite(self, text, addr):
        self.send_response(text, addr, 480, 'Temporarily Unavailable')


def register(ata, expires):
    for _ in range(3):
        ata.reg_ok = False
        ata.reg_event.clear()
        request = ata.registration_request(expires).encode()
        for retry in range(3):
            ata.sip_sock.sendto(request, (ata.cfg['pbx_ip'], ata.cfg['pbx_port']))
            if ata.reg_event.wait(0.5 * 2 ** retry):
                break
        if ata.reg_ok:
            return True
        if not ata.reg_nonce:
            return False
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pbx', required=True)
    parser.add_argument('--login', required=True)
    parser.add_argument('--port', type=int, default=5060)
    args = parser.parse_args()
    password = getpass.getpass('Extension secret: ')
    route = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        route.connect((args.pbx, args.port))
        local_ip = route.getsockname()[0]
    finally:
        route.close()
    ata = SilentProbe(dict(pbx_ip=args.pbx, pbx_port=args.port, domain=args.pbx,
                          login=args.login, password=password, local_ip=local_ip,
                          sip_port=0))
    ata.start_sip(register=False)
    try:
        success = register(ata, 60)
        print('REGISTER', 'PASS' if success else 'FAIL', flush=True)
    finally:
        # Remove our Contact even if a 200 was lost. Expires=0 applies only
        # to this probe's Contact, never wildcard/unregister-all.
        removed = register(ata, 0)
        print('DEREGISTER', 'PASS' if removed else 'FAIL', flush=True)
        ata.stop.set()
        ata.sip_sock.close()
    if not success or not removed:
        sys.exit(1)


if __name__ == '__main__':
    main()
