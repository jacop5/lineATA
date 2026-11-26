"""Independent adversarial acceptance tests for the planned graceful-shutdown
API (TASK-0018 lead architecture): LinearATA.request_shutdown(signum, frame)
sets shutdown_requested with no I/O; LinearATA.shutdown(timeout=...) performs
bounded, idempotent teardown. Mock-only: no real sockets/USB/network/sleeps.

At baseline (pre-TASK-0018) the API is absent, so these fail with
AttributeError — that is expected feature-absence evidence, not a bug proof.
"""
import queue as queue_mod
import socket as socket_mod
import threading
import time as time_mod
from unittest.mock import Mock, call

import pytest

from bridge.ata import LinearATA, sip_header, sdp_local


class _Delegate:
    """Namespace shim: override selected attributes, delegate the rest."""

    def __init__(self, target, **overrides):
        object.__setattr__(self, '_target', target)
        object.__setattr__(self, '_overrides', overrides)

    def __getattr__(self, name):
        overrides = object.__getattribute__(self, '_overrides')
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, '_target'), name)


class FakeEvent:
    """Deterministic stand-in for threading.Event: records waits, never blocks."""

    def __init__(self):
        self._set = False
        self.waits = []

    def set(self):
        self._set = True

    def clear(self):
        self._set = False

    def is_set(self):
        return self._set

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self._set


class FakeStop:
    def __init__(self):
        self._set = False
        self.waits = []

    def set(self):
        self._set = True

    def is_set(self):
        return self._set

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self._set


class FakeClock:
    """Deterministic clock: every read advances by step; no real waiting."""

    def __init__(self, step=0.05):
        self.t = 1000.0
        self.step = step

    def monotonic(self):
        self.t += self.step
        return self.t

    def time(self):
        return self.t

CFG = {'login': 'test', 'password': 'test-only', 'domain': 'example.invalid',
       'pbx_ip': '127.0.0.1', 'pbx_port': 5060, 'local_ip': '127.0.0.1',
       'sip_port': 5062}
ADDR = ('127.0.0.1', 5060)
OUR_CONTACT = 'sip:test@127.0.0.1:5062'


@pytest.fixture
def ata(monkeypatch):
    """Started (register=False) LinearATA on a mock socket; threads recorded,
    never really started."""
    started = []

    class FakeThread:
        def __init__(self, *a, **k):
            self.target = k.get('target')
            self.daemon = k.get('daemon')
            self.joined = []
            started.append(self)

        def start(self):
            pass

        def join(self, timeout=None):
            self.joined.append(timeout)

        def is_alive(self):
            return False

    class ThreadingShim:
        Thread = FakeThread

        def __getattr__(self, name):
            return getattr(threading, name)

    sock = Mock()
    sock.getsockname.return_value = ('127.0.0.1', 5062)
    monkeypatch.setattr('bridge.ata.socket.socket', Mock(return_value=sock))
    monkeypatch.setattr('bridge.ata.threading', ThreadingShim())
    a = LinearATA(dict(CFG))
    a.write = Mock()
    a.start_sip(register=False)
    a.fake_sock = sock
    a.fake_threads = started
    return a


def respond(request, status, extra=()):
    """SIP response copying the request's Via/CSeq/Call-ID."""
    req = request.decode() if isinstance(request, bytes) else request
    head, sep, body = req.partition('\r\n\r\n')
    out = [status]
    for line in head.split('\r\n')[1:]:
        if line.lower().startswith('contact:'):
            continue
        out.append(line)
    out.extend(extra)
    return '\r\n'.join(out) + sep + body


def invite(cid='adv-invite', cseq=42):
    body = sdp_local('127.0.0.1', 24000)
    return ('INVITE sip:test@example.invalid SIP/2.0\r\n'
            'Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKadv\r\n'
            'From: <sip:caller@example.invalid>;tag=caller\r\n'
            'To: <sip:test@example.invalid>\r\n'
            'Contact: <sip:caller@127.0.0.1:5060>\r\n'
            f'Call-ID: {cid}\r\nCSeq: {cseq} INVITE\r\n'
            f'Content-Type: application/sdp\r\nContent-Length: {len(body)}\r\n\r\n{body}')


def test_request_shutdown_sets_event_without_any_io(ata):
    assert not ata.shutdown_requested.is_set()
    ata.request_shutdown()
    assert ata.shutdown_requested.is_set() is True
    ata.request_shutdown(15, None)  # signal-handler signature tolerated
    assert ata.shutdown_requested.is_set() is True
    ata.fake_sock.sendto.assert_not_called()   # no SIP traffic
    ata.fake_sock.close.assert_not_called()    # no socket teardown in handler
    ata.write.assert_not_called()              # no USB traffic


def test_shutdown_on_fresh_unstarted_instance_is_safe_and_idempotent():
    a = LinearATA(dict(CFG))  # never started: partial-start tolerance
    a.shutdown(timeout=1.0)
    a.shutdown(timeout=1.0)  # idempotent: no exception, no repeated teardown


def test_shutdown_started_instance_closes_socket_once_and_is_idempotent(ata):
    manager = Mock()
    manager.attach_mock(ata.fake_sock.sendto, 'sendto')
    manager.attach_mock(ata.fake_sock.close, 'close')
    ata.request_shutdown()
    ata.shutdown(timeout=2.0)
    assert ata.fake_sock.close.call_count == 1
    sends_after_first = ata.fake_sock.sendto.call_count
    ata.shutdown(timeout=2.0)  # duplicate shutdown: no repeated final traffic
    assert ata.fake_sock.close.call_count == 1
    assert ata.fake_sock.sendto.call_count == sends_after_first


def test_invite_after_request_is_rejected_480_and_never_rings(ata):
    ata.request_shutdown()
    ata.handle_sip_message(invite(), ADDR)
    payloads = [c.args[0] for c in ata.fake_sock.sendto.call_args_list]
    assert any(p.startswith(b'SIP/2.0 480') for p in payloads)
    assert ata.call is None
    assert ata.ringing is False
    ata.write.assert_not_called()  # no ring/answer USB frames


def test_hook_and_dtmf_after_request_do_not_create_call_or_tones(ata):
    ata.request_shutdown()
    ata.handle_frame(bytes([0xA5, 0x04, 0x01, 0x00]))   # OFFHOOK
    ata.handle_frame(bytes([0xA4, 0x04, ord('5'), 0x00]))  # DTMF '5'
    assert ata.call is None
    assert ata.dialing == ''
    ata.write.assert_not_called()  # no DIAL_TONE / TONE_OFF after request


def test_deregistration_uses_own_contact_expires0_after_usb_stop(ata):
    ata.stop.set()  # global USB stop must NOT preclude SIP Contact removal
    ata.request_shutdown()
    ata.shutdown(timeout=2.0)
    regs = [c.args[0] for c in ata.fake_sock.sendto.call_args_list
            if c.args[0].startswith(b'REGISTER')]
    assert regs, 'expected a deregistration REGISTER'
    assert any(b'expires=0' in r and OUR_CONTACT.encode() in r for r in regs)
    assert all(b'Contact: *' not in r for r in regs)  # never wildcard removal


def test_no_traffic_after_socket_close_and_close_called_once(ata):
    """Narrowed claim (mock listener threads are always 'dead', so call order
    alone cannot prove listener liveness — see the receive-lifetime test): the
    socket is closed exactly once and no sendto happens after close."""
    manager = Mock()
    manager.attach_mock(ata.fake_sock.sendto, 'sendto')
    manager.attach_mock(ata.fake_sock.close, 'close')
    ata.request_shutdown()
    ata.shutdown(timeout=2.0)
    calls = manager.mock_calls
    assert call.close() in calls
    assert calls[-1] == call.close()          # close is the final socket call
    assert calls.count(call.close()) == 1     # exactly once


def test_listener_receives_invite_after_request_until_shutdown(ata):
    """Observable receive-lifetime: a real sip_listener thread on the mock
    socket still answers an INVITE with 480 after request_shutdown, and is
    stopped by shutdown(). Event-driven; no sleeps, no real sockets."""
    q = queue_mod.Queue()
    got_480 = threading.Event()

    def recvfrom(n):
        try:
            return q.get(timeout=2), ADDR
        except queue_mod.Empty:
            raise socket_mod.timeout()

    def sendto(data, addr):
        if data.startswith(b'SIP/2.0 480'):
            got_480.set()

    ata.fake_sock.recvfrom.side_effect = recvfrom
    ata.fake_sock.sendto.side_effect = sendto
    listener = threading.Thread(target=ata.sip_listener, daemon=True)
    listener.start()
    try:
        ata.request_shutdown()
        q.put(invite().encode())
        assert got_480.wait(2)          # listener alive after request: 480 sent
        ata.shutdown(timeout=0.5)
        q.put(b'')                      # wake the parked recvfrom to observe sip_stop
        listener.join(2)
        assert not listener.is_alive()  # shutdown stopped the listener
    finally:
        ata.stop.set()
        q.put(b'')  # wake a blocked recvfrom so the listener can exit
        listener.join(2)


def test_deregistration_answers_401_with_authorization_within_deadline(ata):
    challenge = 'Digest realm="example.invalid", nonce="d1", qop="auth"'

    def sendto(data, addr):
        if data.startswith(b'REGISTER') and b'Authorization' not in data:
            ata.handle_sip_message(respond(
                data, 'SIP/2.0 401 Unauthorized',
                extra=[f'WWW-Authenticate: {challenge}']), addr)

    ata.fake_sock.sendto.side_effect = sendto
    ata.request_shutdown()
    ata.shutdown(timeout=2.0)
    regs = [c.args[0] for c in ata.fake_sock.sendto.call_args_list
            if c.args[0].startswith(b'REGISTER')]
    assert any(b'expires=0' in r and b'Authorization: Digest' in r for r in regs)


def test_deregistration_407_uses_proxy_authorization(ata):
    def sendto(data, addr):
        if data.startswith(b'REGISTER') and b'Authorization' not in data and b'Proxy-Authorization' not in data:
            ata.handle_sip_message(respond(
                data, 'SIP/2.0 407 Proxy Authentication Required',
                extra=['Proxy-Authenticate: Digest realm="example.invalid", nonce="d7"']), addr)

    ata.fake_sock.sendto.side_effect = sendto
    ata.request_shutdown()
    ata.shutdown(timeout=2.0)
    regs = [c.args[0] for c in ata.fake_sock.sendto.call_args_list
            if c.args[0].startswith(b'REGISTER')]
    assert any(b'expires=0' in r and b'Proxy-Authorization: Digest' in r for r in regs)


def test_deregistration_401_then_authenticated_200_succeeds_with_positive_result(ata):
    """Full challenge->auth->200 flow: the removal REGISTER is challenged once,
    re-sent with auth and expires=0, and shutdown returns a positive result."""
    challenge = 'Digest realm="example.invalid", nonce="d1", qop="auth"'
    answered = []

    def sendto(data, addr):
        if data.startswith(b'REGISTER'):
            if b'Authorization' not in data:
                ata.handle_sip_message(respond(
                    data, 'SIP/2.0 401 Unauthorized',
                    extra=[f'WWW-Authenticate: {challenge}']), addr)
            else:
                ata.handle_sip_message(respond(data, 'SIP/2.0 200 OK'), addr)
                answered.append(data)

    ata.fake_sock.sendto.side_effect = sendto
    ata.request_shutdown()
    res = ata.shutdown(timeout=2.0)
    assert res  # positive final result on the success path
    regs = [c.args[0] for c in ata.fake_sock.sendto.call_args_list
            if c.args[0].startswith(b'REGISTER')]
    assert len([r for r in regs if b'expires=0' in r]) >= 2  # plain + authenticated
    assert answered and b'Authorization: Digest' in answered[0] and b'expires=0' in answered[0]
    ata.fake_sock.close.assert_called_once_with()


def test_deregistration_missing_response_still_releases_within_deadline(ata, monkeypatch):
    """No response fed: shutdown must still finish within its deadline and
    release the socket. Deterministic: fake clock advances per read, fake
    events never block — no real response waits."""
    clock = FakeClock()
    monkeypatch.setattr('bridge.ata.time', _Delegate(
        time_mod, monotonic=clock.monotonic, time=clock.time))
    ata.reg_event = FakeEvent()   # never set: every reg wait returns immediately
    ata.stop = FakeStop()
    ata.request_shutdown()
    t0 = clock.t
    res = ata.shutdown(timeout=0.5)
    elapsed = clock.t - t0
    assert elapsed <= 0.5 + 0.5          # strict deadline honored (fake time)
    assert res is not None               # bounded failure is still a result
    ata.fake_sock.close.assert_called()  # resources released regardless


def test_active_call_hung_up_exactly_once_and_closed_across_duplicate_shutdowns(ata):
    """Active call: shutdown must hang up exactly once (not zero) and close it;
    a duplicate shutdown must not repeat either."""
    call = Mock()
    call.established = True
    call.media = True
    call.media_enable_due = 0
    ata.call = call
    ata.request_shutdown()
    ata.shutdown(timeout=2.0)
    assert call.hangup.call_count == 1
    assert call.close.call_count == 1
    ata.shutdown(timeout=2.0)
    assert call.hangup.call_count == 1  # no duplicate BYE on second shutdown
    assert call.close.call_count == 1


def test_idle_shutdown_sends_no_hangup_frames(ata):
    assert ata.call is None
    ata.request_shutdown()
    ata.shutdown(timeout=2.0)
    ata.write.assert_not_called()  # no idle repeated HANGUP/TONE_OFF


# ------------------------- TASK-0019 adversarial focus: pending INVITE vs shutdown CANCEL
from bridge.ata import SIPCall


def invite_request(call):
    body = sdp_local('127.0.0.1', call.local_rtp)
    return (f'INVITE sip:123@example.invalid SIP/2.0\r\n'
            f'Via: SIP/2.0/UDP 127.0.0.1:5062;branch={call.branch};rport\r\n'
            'Max-Forwards: 70\r\n'
            f"From: <sip:test@example.invalid>;tag={call.from_tag}\r\n"
            'To: <sip:123@example.invalid>\r\n'
            f'Call-ID: {call.call_id}\r\n'
            f'CSeq: {call.cseq} INVITE\r\n'
            'Contact: <sip:test@127.0.0.1:5062>\r\n'
            'Content-Type: application/sdp\r\n'
            f'Content-Length: {len(body)}\r\n\r\n{body}')


def final_response(request, status, body=''):
    req = request.decode() if isinstance(request, bytes) else request
    head, sep, _ = req.partition('\r\n\r\n')
    out = [status]
    for line in head.split('\r\n')[1:]:
        if line.lower().startswith(('to:', 'content-length:', 'content-type:')):
            if line.lower().startswith('to:'):
                line = line + ';tag=remote1'
            else:
                continue
        out.append(line)
    out.append('Contact: <sip:callee@127.0.0.1:5060>')
    if body:
        out.append('Content-Type: application/sdp')
        out.append(f'Content-Length: {len(body)}')
    else:
        out.append('Content-Length: 0')
    return '\r\n'.join(out) + '\r\n\r\n' + body


def pending_outgoing_call(ata):
    call = SIPCall(ata, '123')
    call.sock = ata.fake_sock
    call.remote = ADDR
    call.last_request = invite_request(call)
    call.invite_cseq = call.cseq
    call.prepare_media = Mock()      # stay offline: no real RTP socket
    call.start_media = Mock()
    call.rtp = Mock()
    ata.call = call
    ata.invite_transactions[call.call_id] = call
    return call


def sent_methods(ata):
    methods = []
    for c in ata.fake_sock.sendto.call_args_list:
        data = c.args[0]
        text = data.decode() if isinstance(data, bytes) else data
        methods.append(text.split(' ', 1)[0])
    return methods


def test_cancel_then_late_200_sends_ack_then_bye_exactly_once(ata):
    """Pending outgoing INVITE + shutdown CANCEL racing a late 200: the wire
    must show CANCEL, then ACK, then BYE — a cancelled flag must not suppress
    the BYE required after the racing 200."""
    call = pending_outgoing_call(ata)
    call.hangup()  # shutdown path on a pending call: sends CANCEL, stays open
    assert sent_methods(ata) == ['CANCEL']
    resp = final_response(call.last_request, 'SIP/2.0 200 OK',
                          body=sdp_local('127.0.0.1', 24000))
    ata.handle_sip_message(resp, ADDR)          # routes into call.responses
    assert call.handle_responses(timeout=0.5) is False
    methods = sent_methods(ata)
    print('wire after racing 200:', methods)
    assert methods.count('CANCEL') == 1
    assert methods.count('ACK') == 1
    assert methods.count('BYE') == 1
    assert methods == ['CANCEL', 'ACK', 'BYE']


def test_late_200_after_cancel_then_second_hangup_sends_no_duplicate_bye(ata):
    """After the racing 200 already produced ACK+BYE, another hangup (e.g. the
    shutdown finalizer) must not emit a second BYE on the wire."""
    call = pending_outgoing_call(ata)
    call.hangup()
    resp = final_response(call.last_request, 'SIP/2.0 200 OK',
                          body=sdp_local('127.0.0.1', 24000))
    ata.handle_sip_message(resp, ADDR)
    call.handle_responses(timeout=0.5)
    call.hangup()  # finalizer runs hangup again on the same call
    methods = sent_methods(ata)
    print('wire after duplicate hangup:', methods)
    assert methods.count('BYE') == 1
    assert methods.count('ACK') == 1


def test_200_before_cancel_sends_bye_once_and_no_cancel(ata):
    """Normal answer then shutdown: ACK on the 200, exactly one BYE at
    shutdown, and no CANCEL."""
    call = pending_outgoing_call(ata)
    resp = final_response(call.last_request, 'SIP/2.0 200 OK',
                          body=sdp_local('127.0.0.1', 24000))
    ata.handle_sip_message(resp, ADDR)
    assert call.handle_responses(timeout=0.5) is True
    call.hangup()  # shutdown on an established call
    methods = sent_methods(ata)
    print('wire established-then-shutdown:', methods)
    assert methods.count('ACK') == 1
    assert methods.count('CANCEL') == 0
    assert methods.count('BYE') == 1


def test_487_after_cancel_sends_ack_but_no_bye(ata):
    """CANCEL answered by 487: transaction ACK only; no dialog, so no BYE."""
    call = pending_outgoing_call(ata)
    call.hangup()
    resp = final_response(call.last_request, 'SIP/2.0 487 Request Terminated')
    ata.handle_sip_message(resp, ADDR)
    assert call.handle_responses(timeout=0.5) is False
    methods = sent_methods(ata)
    print('wire after 487:', methods)
    assert methods.count('CANCEL') == 1
    assert methods.count('ACK') == 1
    assert methods.count('BYE') == 0


def test_shutdown_api_pending_invite_race_wire_counts(ata):
    """Planned-API level: request_shutdown + shutdown on a pending outgoing
    INVITE. A real bounded worker consumes call.responses BEFORE shutdown; the
    racing 200 is injected during the deregistration REGISTER send (after
    CANCEL, socket still open) and the REGISTER 200 is fed only once BYE was
    observed, so the window is genuinely successful and BYE provably precedes
    socket closure. Wire: exactly one CANCEL, one ACK, one BYE."""
    call = pending_outgoing_call(ata)
    bye_seen = threading.Event()
    state_at_bye = {}
    fed_invite = []
    fed_reg = []

    def sendto(data, addr):
        text = data.decode() if isinstance(data, bytes) else data
        if text.startswith('BYE'):
            bye_seen.set()
            state_at_bye['socket_open'] = (ata.fake_sock.close.call_count == 0
                                            and not ata.sip_stop.is_set())
        if text.startswith('REGISTER'):
            if not fed_invite:
                fed_invite.append(True)
                # socket open, listener alive, CANCEL already sent:
                assert not ata.sip_stop.is_set()
                assert ata.fake_sock.close.call_count == 0
                assert sent_methods(ata).count('CANCEL') == 1
                ata.handle_sip_message(
                    final_response(call.last_request, 'SIP/2.0 200 OK',
                                   body=sdp_local('127.0.0.1', 24000)), addr)
            if bye_seen.is_set() and not fed_reg:
                fed_reg.append(True)
                ata.handle_sip_message(respond(data, 'SIP/2.0 200 OK'), addr)

    ata.fake_sock.sendto.side_effect = sendto
    worker = threading.Thread(target=call.handle_responses, args=(5,), daemon=True)
    worker.start()  # real bounded worker, started before shutdown
    try:
        ata.request_shutdown()
        res = ata.shutdown(timeout=2.0)
        worker.join(2)
        assert not worker.is_alive(), 'worker did not finish bounded'
        assert fed_invite and fed_reg, 'responses not injected during the window'
        assert bye_seen.is_set(), 'BYE not observed'
        assert state_at_bye.get('socket_open') is True, 'BYE sent after socket closed'
        methods = sent_methods(ata)
        assert methods.count('CANCEL') == 1
        assert methods.count('ACK') == 1
        assert methods.count('BYE') == 1     # single BYE, never duplicated
        assert 'deregister:ok' in str(res)   # window genuinely succeeded
        assert call.closed is True
    finally:
        if worker.is_alive():
            worker.join(2)
        if not call.closed:
            call.close()


def test_second_hangup_on_pending_call_must_not_duplicate_cancel(ata):
    """Scoped baseline probe via existing SIPCall API: hangup() twice on a
    still-pending outgoing call must emit exactly one CANCEL on the wire."""
    call = pending_outgoing_call(ata)
    call.hangup()
    call.hangup()
    methods = sent_methods(ata)
    print('pending double-hangup wire:', methods)
    assert methods.count('CANCEL') == 1


def test_second_hangup_on_established_call_must_not_duplicate_bye(ata):
    """Scoped baseline probe via existing SIPCall API: hangup() twice on an
    established call must emit exactly one BYE on the wire."""
    call = pending_outgoing_call(ata)
    resp = final_response(call.last_request, 'SIP/2.0 200 OK',
                          body=sdp_local('127.0.0.1', 24000))
    ata.handle_sip_message(resp, ADDR)
    call.handle_responses(timeout=0.5)
    call.hangup()
    call.hangup()
    methods = sent_methods(ata)
    print('established double-hangup wire:', methods)
    assert methods.count('BYE') == 1
