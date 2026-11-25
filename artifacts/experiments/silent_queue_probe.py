#!/usr/bin/env python3
"""20-second runtime queue check: no SIP, no hook actions, no ring commands."""
import time
from ata import LinearATA, EP_OUT, reset_to_bootloader


class HeartbeatOnlyDevice:
    def __init__(self, dev):
        self.dev = dev

    def write(self, ep, data, timeout):
        if ep != EP_OUT or data not in (b'\xa0\x03\x00', b'\xa0\x03\x01'):
            raise RuntimeError('silent probe blocked a non-heartbeat write')
        return self.dev.write(ep, data, timeout)

    def read(self, *args):
        return self.dev.read(*args)


class SilentATA(LinearATA):
    def handle_frame(self, frame):
        if self.usb_commands.acknowledge(frame):
            self.acknowledged += 1
        elif frame[0] in (0xa4, 0xa5):
            self.log('ignored handset event', frame.hex())


if __name__ == '__main__':
    import threading
    import usb.util
    ata = SilentATA({'cold': True, 'dial_timeout': 3})
    if not reset_to_bootloader() or not ata.connect() or not ata.usb_init():
        raise SystemExit('initialization failed')
    original_device = ata.dev
    ata.dev = HeartbeatOnlyDevice(original_device)
    ata.usb_active = True
    ata.acknowledged = 0
    ata.usb_owner = threading.get_ident()
    timer = threading.Timer(20, ata.stop.set)
    timer.start()
    try:
        ata._usb_loop()
        if ata.acknowledged < 19 or ata.usb_commands.pending:
            raise RuntimeError('heartbeat responses missing')
        print('PASS runtime heartbeat ACKs:', ata.acknowledged, flush=True)
    finally:
        timer.cancel()
        usb.util.dispose_resources(original_device)
