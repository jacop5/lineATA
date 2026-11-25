"""A simulated PBX and recording USB transport; no real phone can ring."""
import re
from unittest.mock import Mock

import pytest

from bridge.ata import LinearATA, SIPCall, parse_sdp, sdp_local, parse_digest


@pytest.fixture
def rtp_socket(monkeypatch):
    sock = Mock()
    sock.getsockname.return_value = ('127.0.0.1', 24001)
    monkeypatch.setattr('bridge.ata.socket.socket', Mock(return_value=sock))
    return sock


def make_ata():
    ata = LinearATA({'login': 'test', 'password': 'test-only',
                     'domain': 'example.invalid', 'pbx_ip': '127.0.0.1',
                     'pbx_port': 5060, 'local_ip': '127.0.0.1', 'sip_port': 5062})
    ata.sip_local_port = 5062
    ata.sip_sock = Mock()
    ata.write = Mock()  # all physical USB commands are recorded, never executed
    return ata


def invite(body=None, cid='inbound-test', cseq=42):
    if body is None:
        body = sdp_local('127.0.0.1', 24000)
    return ('INVITE sip:test@example.invalid SIP/2.0\r\n'
            'Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest\r\n'
            'From: <sip:caller@example.invalid>;tag=caller\r\n'
            'To: <sip:test@example.invalid>\r\n'
            'Contact: <sip:caller@127.0.0.1:5060>\r\n'
            f'Call-ID: {cid}\r\nCSeq: {cseq} INVITE\r\n'
            f'Content-Type: application/sdp\r\nContent-Length: {len(body)}\r\n\r\n{body}')


def test_audio_media_connection_overrides_session_and_video_connections():
    sdp = ('v=0\r\nc=IN IP4 192.0.2.1\r\n'
           'm=video 4000 RTP/AVP 96\r\nc=IN IP4 192.0.2.2\r\n'
           'm=audio 5000 RTP/AVP 0\r\nc=IN IP4 192.0.2.3\r\n')
    assert parse_sdp(sdp) == ('192.0.2.3', 5000)


@pytest.mark.parametrize('body', [
    'v=0\r\nm=audio 24000 RTP/AVP 0\r\n',
    'v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio 0 RTP/AVP 0\r\n',
    'v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio 99999 RTP/AVP 0\r\n',
    'v=0\r\nc=IN IP4 invalid\r\nm=audio 24000 RTP/AVP 0\r\n',
    'v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio 24000 RTP/AVP 8\r\n',
])
def test_unusable_offer_rejected_before_usb_commands(body):
    ata = make_ata()
    ata.on_invite(invite(body), ('127.0.0.1', 5060))
    assert ata.sip_sock.sendto.call_args.args[0].startswith(b'SIP/2.0 488')
    ata.write.assert_not_called()
    assert ata.call is None


def test_duplicate_invite_reuses_response_without_preparing_usb_again():
    ata = make_ata()
    req = invite()
    ata.on_invite(req, ('127.0.0.1', 5060))
    initial_writes = ata.write.call_count
    response = ata.sip_sock.sendto.call_args.args[0]
    ata.on_invite(req, ('127.0.0.1', 5060))
    assert ata.sip_sock.sendto.call_args.args[0] == response
    assert ata.write.call_count == initial_writes


def test_incoming_answer_advertises_a_bound_rtp_socket(rtp_socket):
    ata = make_ata()
    ata.on_invite(invite(), ('127.0.0.1', 5060))
    call = ata.call
    call.media_loop = Mock()
    try:
        ata.ringing = False
        ata.answer_incoming()
        response = ata.sip_sock.sendto.call_args.args[0].decode()
        advertised = int(re.search(r'm=audio (\d+)', response).group(1))
        assert call.rtp.getsockname() == ('127.0.0.1', advertised)
        rtp_socket.bind.assert_called_once_with(('127.0.0.1', 0))
        assert call.established
    finally:
        call.close()


def test_outgoing_rtp_port_is_reserved_before_invite_is_sent(rtp_socket):
    ata = make_ata()
    call = ata.call = SIPCall(ata, '69')
    call.handle_responses = Mock(return_value=False)
    try:
        call.invite()
        request = ata.sip_sock.sendto.call_args.args[0].decode()
        advertised = int(re.search(r'm=audio (\d+)', request).group(1))
        assert call.rtp is not None
        assert call.rtp.getsockname() == ('127.0.0.1', advertised)
        rtp_socket.bind.assert_called_once_with(('127.0.0.1', 0))
    finally:
        call.close()


def request_method(req, method, tag=None):
    request = req.replace('INVITE ', method + ' ', 1).replace('42 INVITE', '42 ' + method)
    if tag:
        request = request.replace('To: <sip:test@example.invalid>',
                                  'To: <sip:test@example.invalid>;tag=' + tag)
    return request


def test_cancelled_invite_retransmission_does_not_restart_ringing():
    ata = make_ata()
    req = invite()
    addr = ('127.0.0.1', 5060)
    ata.handle_sip_message(req, addr)
    ata.handle_sip_message(request_method(req, 'CANCEL'), addr)
    assert ata.call is None
    assert not ata.ringing
    writes_after_cancel = ata.write.call_count
    ata.handle_sip_message(req, addr)
    assert ata.sip_sock.sendto.call_args.args[0].startswith(b'SIP/2.0 487')
    assert ata.write.call_count == writes_after_cancel
    assert ata.call is None


def test_incoming_200_retransmits_until_ack(rtp_socket):
    ata = make_ata()
    req = invite()
    addr = ('127.0.0.1', 5060)
    ata.handle_sip_message(req, addr)
    call = ata.call
    call.media_loop = Mock()
    try:
        ata.ringing = False
        ata.answer_incoming()
        response = call.last_response
        sends = ata.sip_sock.sendto.call_count
        ata.sip_tick(call.final_due + .001)
        assert ata.sip_sock.sendto.call_count == sends + 1
        assert ata.sip_sock.sendto.call_args.args[0] == response
        ata.handle_sip_message(request_method(req, 'ACK', call.from_tag), addr)
        assert call.ack_received
        sends = ata.sip_sock.sendto.call_count
        ata.sip_tick(call.final_due + 1)
        assert ata.sip_sock.sendto.call_count == sends
    finally:
        call.close()


def test_bye_retransmission_gets_200_without_second_hardware_teardown():
    ata = make_ata()
    req = invite()
    addr = ('127.0.0.1', 5060)
    ata.handle_sip_message(req, addr)
    bye = request_method(req, 'BYE', ata.call.from_tag)
    ata.handle_sip_message(bye, addr)
    writes = ata.write.call_count
    ata.handle_sip_message(bye, addr)
    assert ata.sip_sock.sendto.call_args.args[0].startswith(b'SIP/2.0 200')
    assert ata.write.call_count == writes


def test_late_response_reaches_cancelled_transaction_without_active_call():
    ata = make_ata()
    call = SIPCall(ata, '69')
    call.cancelled = True
    ata.invite_transactions[call.call_id] = call
    response = (f'SIP/2.0 200 OK\r\nCall-ID: {call.call_id}\r\n'
                'CSeq: 1 INVITE\r\nTo: <sip:69@example.invalid>;tag=b\r\n\r\n')
    ata.handle_sip_message(response, ('127.0.0.1', 5060))
    assert call.responses.get_nowait()[0] == response


def test_cached_outbound_ack_is_resent_after_call_object_is_gone():
    ata = make_ata()
    response = ('SIP/2.0 200 OK\r\nCall-ID: completed\r\nCSeq: 1 INVITE\r\n'
                'To: <sip:69@example.invalid>;tag=b\r\n\r\n')
    import time
    ata.invite_acks[('completed', '1', '<sip:69@example.invalid>;tag=b')] = (
        b'ACK cached', time.monotonic() + 32)
    ata.handle_sip_message(response, ('127.0.0.1', 5060))
    assert ata.sip_sock.sendto.call_args.args[0] == b'ACK cached'


def test_case_insensitive_compact_headers_do_not_change_request_uri():
    ata = make_ata()
    ata.on_invite = Mock()
    req = invite().replace('Call-ID:', 'i:').replace('From:', 'f:').replace('CSeq:', 'cseq:')
    ata.handle_sip_message(req, ('127.0.0.1', 5060))
    normalized = ata.on_invite.call_args.args[0]
    assert normalized.split('\r\n')[0] == req.split('\r\n')[0]
    assert 'Call-ID: inbound-test\r\n' in normalized


def test_old_call_cleanup_cannot_erase_new_calls_audio():
    ata = make_ata()
    old = SIPCall(ata, '69')
    ata.call = SIPCall(ata, '70')
    ata.push_usb_audio(b'new call audio')
    old.close()
    assert ata.in_audio == b'new call audio'


def test_cancel_racing_with_200_is_acked_then_terminated():
    ata = make_ata()
    call = SIPCall(ata, '69')
    call.cancelled = True
    call.sock = ata.sip_sock
    call.remote = ('127.0.0.1', 5060)
    ata.invite_transactions[call.call_id] = call
    response = ('SIP/2.0 200 OK\r\n'
                f'Call-ID: {call.call_id}\r\nCSeq: 1 INVITE\r\n'
                'From: <sip:test@example.invalid>;tag=local\r\n'
                'To: <sip:69@example.invalid>;tag=remote\r\n'
                'Contact: <sip:69@127.0.0.1:5060>\r\n\r\n' + sdp_local('127.0.0.1', 24000))
    ata.handle_sip_message(response, call.remote)
    assert call.handle_responses(timeout=.5) is False
    messages = [c.args[0] for c in ata.sip_sock.sendto.call_args_list]
    assert messages[0].startswith(b'ACK sip:69@127.0.0.1:5060 SIP/2.0')
    assert messages[1].startswith(b'BYE sip:69@127.0.0.1:5060 SIP/2.0')
    assert call.closed
    assert not call.media


def test_digest_auth_known_vector_and_quoted_qop_list(monkeypatch):
    value = Mock()
    value.hex = '0a4f113b'
    monkeypatch.setattr('bridge.ata.uuid.uuid4', lambda: value)
    challenge = ('Digest realm="testrealm@host.com", qop="auth,auth-int", '
                 'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", algorithm=MD5')
    result = parse_digest(challenge, 'GET', '/dir/index.html', 'Mufasa', 'Circle Of Life')
    assert 'response="6629fae49393a05397450978507c4ef1"' in result
    assert 'qop=auth,' in result


@pytest.mark.parametrize('fields', ['qop="auth-int"', 'algorithm=SHA-256'])
def test_unsupported_digest_is_not_mislabeled_as_md5(fields):
    with pytest.raises(ValueError, match='unsupported'):
        parse_digest('Digest realm="test", nonce="test", ' + fields,
                     'REGISTER', 'sip:example.invalid', 'test', 'test')
