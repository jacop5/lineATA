"""Opt-in real UDP loopback test; synthetic audio, no PBX/USB access."""
import os
import socket
import struct
import queue
import re
import threading
import time

import pytest

from bridge.ata import LinearATA, SIPCall
from bridge.media import pcmu_payload
import usb.core


@pytest.mark.skipif(os.environ.get('LINEATA_LOOPBACK_TEST') != '1',
                    reason='set LINEATA_LOOPBACK_TEST=1 where UDP loopback is permitted')
def test_two_way_rtp_bytes_and_media_shutdown():
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(('127.0.0.1', 0))
    peer.settimeout(1)
    ata = LinearATA({'local_ip': '127.0.0.1'})
    call = ata.call = SIPCall(ata, 'loopback')
    call.remote_ip, call.remote_rtp = peer.getsockname()
    try:
        call.prepare_media()
        local = call.rtp.getsockname()
        call.start_media()
        outbound = bytes(range(160))
        ata.push_usb_audio(outbound)
        packet, source = peer.recvfrom(2048)
        assert source == local
        assert pcmu_payload(packet) == outbound
        inbound = bytes(reversed(range(160)))
        # RTP with one CSRC, extension and four padding bytes.
        header = struct.pack('!BBHII', 0xb1, 0, 1, 160, 42)
        peer.sendto(header + b'CSRC' + struct.pack('!HH', 0xbeef, 1)
                    + b'EXTN' + inbound + b'\0\0\0\4', local)
        deadline = time.monotonic() + 1
        received = None
        while time.monotonic() < deadline and received is None:
            received = ata.take_rtp_audio()
            if received is None:
                time.sleep(.005)
        assert received == inbound
    finally:
        call.close()
        if call.thread:
            call.thread.join(timeout=1)
            assert not call.thread.is_alive()
        peer.close()


@pytest.mark.skipif(os.environ.get('LINEATA_LOOPBACK_TEST') != '1',
                    reason='set LINEATA_LOOPBACK_TEST=1 where UDP loopback is permitted')
def test_sip_usb_rtp_integration_with_simulated_handset():
    """Actual SIP/media threads and UDP, but a completely synthetic USB device."""
    class SyntheticUSB:
        def __init__(self):
            self.incoming = queue.Queue()
            self.audio_received = threading.Event()
            self.voice = None

        def read(self, ep, size, timeout):
            try:
                return self.incoming.get(timeout=timeout / 1000)
            except queue.Empty:
                raise usb.core.USBTimeoutError('synthetic USB timeout')

        def write(self, ep, data, timeout):
            if data[:2] == b'\x00\xa2':
                self.voice = data[2:]
                self.audio_received.set()
            else:
                # Register reads return a value; other controls get an ACK.
                response = (data[:1] + b'\x03\x44' if data[:1] in (b'\x40', b'\x60')
                            else data[:1] + b'\x02')
                self.incoming.put(response)
            return len(data)

    sip_peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp_peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for peer in (sip_peer, rtp_peer):
        peer.bind(('127.0.0.1', 0))
        peer.settimeout(2)
    ata = LinearATA(dict(local_ip='127.0.0.1', sip_port=0, login='test',
                         domain='example.invalid', dial_timeout=3))
    ata.dev = SyntheticUSB()
    ata.usb_active = True
    ata.start_sip(register=False)  # no real PBX or registration

    def run_usb():
        ata.usb_owner = threading.get_ident()
        ata._usb_loop()

    usb_thread = threading.Thread(target=run_usb)
    usb_thread.start()
    local_sip = ('127.0.0.1', ata.sip_local_port)

    def receive_start(prefix):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            msg, addr = sip_peer.recvfrom(65535)
            if msg.startswith(prefix):
                return msg.decode(), addr
        raise AssertionError('missing SIP message ' + repr(prefix))

    def await_condition(condition):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.005)
        raise AssertionError('state transition timed out')

    try:
        for cycle in range(2):
            cid = 'synthetic-cycle-' + str(cycle)
            peer_port = sip_peer.getsockname()[1]
            body = ('v=0\r\nc=IN IP4 127.0.0.1\r\n'
                    f'm=audio {rtp_peer.getsockname()[1]} RTP/AVP 0\r\n')
            headers = (f'Via: SIP/2.0/UDP 127.0.0.1:{peer_port};branch=z9hG4bK{cycle}\r\n'
                       f'From: <sip:caller@example.invalid>;tag=peer{cycle}\r\n'
                       'To: <sip:test@example.invalid>\r\n'
                       f'Contact: <sip:caller@127.0.0.1:{peer_port}>\r\n'
                       f'Call-ID: {cid}\r\n')
            request = ('INVITE sip:test@example.invalid SIP/2.0\r\n' + headers
                       + f'CSeq: 42 INVITE\r\nContent-Length: {len(body)}\r\n\r\n' + body)
            sip_peer.sendto(request.encode(), local_sip)
            receive_start(b'SIP/2.0 180')
            ata.dev.incoming.put(bytes.fromhex('a5040100'))
            answer, _ = receive_start(b'SIP/2.0 200')
            call = ata.call
            tagged_headers = headers.replace('To: <sip:test@example.invalid>',
                                             'To: <sip:test@example.invalid>;tag=' + call.from_tag)
            ack = 'ACK sip:test@example.invalid SIP/2.0\r\n' + tagged_headers + 'CSeq: 42 ACK\r\n\r\n'
            sip_peer.sendto(ack.encode(), local_sip)
            await_condition(lambda: call.ack_received)
            if cycle == 0:
                # Both real UDP media directions traverse the simulated USB loop.
                outbound = b'\x7e' * 160
                ata.dev.incoming.put(b'\x00\xa2' + outbound)
                packet, _ = rtp_peer.recvfrom(2048)
                assert pcmu_payload(packet) == outbound
                port = int(re.search(r'm=audio (\d+)', answer).group(1))
                inbound = b'\xff' * 160
                rtp_peer.sendto(struct.pack('!BBHII', 0x80, 0, 1, 160, 42) + inbound,
                                ('127.0.0.1', port))
                assert ata.dev.audio_received.wait(1)
                assert ata.dev.voice == inbound
                ata.dev.incoming.put(bytes.fromhex('a5040000'))
                bye, _ = receive_start(b'BYE ')
                assert f'Call-ID: {cid}\r\n' in bye
                assert f'To: <sip:caller@example.invalid>;tag=peer{cycle}\r\n' in bye
            else:
                # The repeated call ends from the peer instead.
                bye = 'BYE sip:test@example.invalid SIP/2.0\r\n' + tagged_headers + 'CSeq: 43 BYE\r\n\r\n'
                sip_peer.sendto(bye.encode(), local_sip)
                receive_start(b'SIP/2.0 200')
            await_condition(lambda: ata.call is None)
            assert not call.media
    finally:
        ata.stop.set()
        if ata.call:
            ata.call.close()
        ata.sip_sock.close()
        usb_thread.join(timeout=1)
        assert not usb_thread.is_alive()
        sip_peer.close()
        rtp_peer.close()
