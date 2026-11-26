"""Opt-in actual UDP DTMF transport with synthetic audio, no USB/PBX/calls."""
import os
import socket
import struct
import time

import pytest

from bridge.ata import LinearATA, SIPCall, parse_sdp, parse_sdp_dtmf, sdp_local
from bridge.media import pcmu_payload


@pytest.mark.skipif(os.environ.get('LINEATA_LOOPBACK_TEST') != '1',
                    reason='requires explicitly enabled localhost UDP')
def test_negotiated_events_and_pcmu_share_actual_udp_stream():
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(('127.0.0.1', 0))
    peer.settimeout(0.3)
    ata = LinearATA({'local_ip': '127.0.0.1'})
    call = ata.call = SIPCall(ata, 'synthetic')
    records = []
    try:
        # Real SDP selection and RTP sender, without a SIP call to any PBX.
        offer = sdp_local(*peer.getsockname(), dtmf_pt=110)
        call.remote_ip, call.remote_rtp = parse_sdp(offer)
        call.dtmf_pt, call.dtmf_events = parse_sdp_dtmf(offer)
        call.start_media()
        audio = bytes(range(160))
        ata.push_usb_audio(audio)
        assert call.send_dtmf('#')
        deadline = time.monotonic() + 2
        ends = 0
        received_audio = []
        while time.monotonic() < deadline and (ends < 3 or not received_audio):
            try:
                packet, source = peer.recvfrom(2048)
            except socket.timeout:
                continue
            assert source == call.rtp.getsockname()
            records.append(packet)
            payload = pcmu_payload(packet)
            if payload is not None:
                received_audio.append(payload)
            elif packet[1] & 127 == 110:
                assert len(packet) == 16
                assert packet[12] == 11
                ends += bool(packet[13] & 128)
        events = [p for p in records if p[1] & 127 == 110]
        assert len(events) == 8 and ends == 3
        assert received_audio == [audio]
        assert len({p[8:12] for p in records}) == 1
        assert len({p[4:8] for p in events}) == 1
        assert [bool(p[1] & 128) for p in events] == [True] + [False] * 7
        assert [struct.unpack('!H', p[14:16])[0] for p in events] == [
            160, 320, 480, 640, 800, 800, 800, 800]
        seqs = [struct.unpack('!H', p[2:4])[0] for p in records]
        assert all((b - a) & 65535 == 1 for a, b in zip(seqs, seqs[1:]))
    finally:
        call.close()
        if call.thread:
            call.thread.join(2)
            assert not call.thread.is_alive()
        peer.close()
