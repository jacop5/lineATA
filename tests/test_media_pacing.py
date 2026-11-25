"""Exercise actual media loops with a clock that includes receive overhead."""
from types import SimpleNamespace
from unittest.mock import Mock
import socket
import threading

from bridge.ata import LinearATA, SIPCall


class Clock:
    value = 0.0

    def now(self):
        return self.value


def test_usb_loop_keeps_50hz_despite_read_overhead(monkeypatch):
    clock = Clock()
    monkeypatch.setattr('bridge.ata.time.monotonic', clock.now)
    ata = LinearATA({'dial_timeout': 3})
    ata.log = Mock()
    ata.call = SimpleNamespace(media=True, media_enable_due=0)
    ata.dev = Mock()
    ata.usb_active = True
    ata.usb_owner = threading.get_ident()
    ata.queue_rtp_audio(b'\xff' * (160 * 100))

    def read(timeout=5):
        clock.value += timeout / 1000 + .0005
        if clock.value >= 1:
            ata.stop.set()
        return bytes.fromhex('a002')

    ata.read = read
    ata._usb_loop()
    assert 48 <= ata.stat_out <= 52


def test_rtp_loop_keeps_50hz_despite_receive_overhead(monkeypatch):
    clock = Clock()
    monkeypatch.setattr('bridge.ata.time.monotonic', clock.now)
    ata = LinearATA({})
    ata.take_usb_audio = Mock(return_value=b'\xff' * 160)
    call = SIPCall(ata, 'synthetic')
    call.remote_ip, call.remote_rtp = '127.0.0.1', 24000
    call.media = True
    timeout = [.005]
    call.rtp = Mock()
    call.rtp.settimeout.side_effect = lambda value: timeout.__setitem__(0, value)

    def receive(_):
        clock.value += timeout[0] + .0005
        if clock.value >= 1:
            call.media = False
        raise socket.timeout()

    call.rtp.recvfrom.side_effect = receive
    call.media_loop()
    assert 48 <= call.rtp.sendto.call_count <= 52
