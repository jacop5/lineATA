"""Regressions for the current daemon, independent of USB hardware."""
import struct
from unittest.mock import Mock

import pytest
import usb.core

from bridge.ata import LinearATA, SIPCall
from bridge.framing import FrameDecoder
from bridge.media import pcmu_payload


def test_hook_is_preserved_among_fragmented_audio_and_ack():
    voice = b'\x00\xa2' + bytes(range(160))
    hook = bytes.fromhex('a5040000')
    stream = voice + bytes.fromhex('a002') + hook + voice
    for chunk_size in (1, 3, 64, 162, 4096):
        decoder = FrameDecoder()
        frames = []
        for offset in range(0, len(stream), chunk_size):
            frames.extend(decoder.feed(stream[offset:offset + chunk_size]))
        assert frames == [voice, bytes.fromhex('a002'), hook, voice]
        assert not decoder.pending


def test_voice_payload_cannot_be_interpreted_as_hook_event():
    voice = b'\x00\xa2' + bytes.fromhex('a5040000') * 40
    assert FrameDecoder().feed(voice) == [voice]


def test_bad_length_fails_visibly():
    with pytest.raises(ValueError):
        FrameDecoder().feed(b'\xa5\x00')


def test_transport_disconnect_is_not_a_timeout():
    ata = LinearATA({})
    ata.dev = Mock()
    ata.dev.read.side_effect = usb.core.USBError('disconnected', errno=19)
    with pytest.raises(usb.core.USBError):
        ata.read()
    ata.dev.read.side_effect = usb.core.USBTimeoutError('timeout')
    assert ata.read() is None


def test_in_call_onhook_sends_bye_and_releases_call():
    ata = LinearATA({})
    ata.write = Mock()
    call = ata.call = Mock()
    ata.handle_frame(bytes.fromhex('a5040000'))
    call.hangup.assert_called_once()
    assert ata.call is None
    assert not ata.ports[1]['hook']


def test_in_call_digits_do_not_originate_another_call():
    ata = LinearATA({})
    ata.ports[1]['hook'] = True
    ata.call = Mock()
    ata.try_dial = Mock()
    ata.write = Mock()
    ata.handle_frame(bytes.fromhex('a4042300'))
    ata.try_dial.assert_not_called()
    ata.write.assert_not_called()


def test_rtp_csrc_extension_and_padding_are_removed():
    header = struct.pack('!BBHII', 0xb1, 0x80, 5, 160, 42)
    packet = header + b'CSRC' + struct.pack('!HH', 0xbeef, 1) + b'EXTN'
    packet += b'\xff' * 160 + b'\0\0\0\4'
    assert pcmu_payload(packet) == b'\xff' * 160
    assert pcmu_payload(packet[:18]) is None
    assert pcmu_payload(bytes([0x80, 8]) + packet[2:]) is None


def test_cancel_200_cannot_establish_the_invite_dialog():
    ata = LinearATA({'login': 'test', 'domain': 'example.invalid',
                     'local_ip': '127.0.0.1'})
    ata.sip_local_port = 5062
    call = ata.call = SIPCall(ata, '69')
    call.send = Mock()
    call.start_media = Mock()
    call.sock = Mock()
    call.remote = ('127.0.0.1', 5060)
    call.cancelled = True
    call.last_request = (
        'INVITE sip:69@example.invalid SIP/2.0\r\n'
        'Via: SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKtest\r\n'
        'From: <sip:test@example.invalid>;tag=a\r\n'
        'To: <sip:69@example.invalid>\r\n'
        f'Call-ID: {call.call_id}\r\nCSeq: 1 INVITE\r\n\r\n')
    for code, method in ((200, 'CANCEL'), (487, 'INVITE')):
        call.responses.put((
            f'SIP/2.0 {code} Test\r\nTo: <sip:69@example.invalid>;tag=b\r\n'
            f'CSeq: 1 {method}\r\n\r\n', call.remote))
    assert call.handle_responses(timeout=0.5) is False
    assert not call.established
    assert all(not c.args[0].startswith('BYE ') for c in call.send.call_args_list)
