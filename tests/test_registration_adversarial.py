"""Adversarial REGISTER tests for LinearATA: mocked socket/threads, no network.

Every response is crafted by copying the actual request's Via/CSeq/Call-ID
(see respond()), the way a compliant PBX answers. Only public surface is
exercised: start_sip(register=False), registration_request, handle_sip_message
plus the existing reg_ok / reg_nonce / reg_event state fields.
"""
import re
import threading
from unittest.mock import Mock

import pytest

from bridge.ata import LinearATA, sip_header

CFG = {'login': 'test', 'password': 'test-only', 'domain': 'example.invalid',
       'pbx_ip': '127.0.0.1', 'pbx_port': 5060, 'local_ip': '127.0.0.1',
       'sip_port': 5062}
ADDR = ('127.0.0.1', 5060)


@pytest.fixture
def ata(monkeypatch):
    """LinearATA with start_sip(register=False) run against a mock socket and
    fake threads, so no real socket, listener thread or registrar is created."""
    started = []

    class FakeThread:
        def __init__(self, *args, **kwargs):
            self.target = kwargs.get('target')
            self.daemon = kwargs.get('daemon')
            self.started = False
            started.append(self)

        def start(self):
            self.started = True

        def join(self, timeout=None):
            return None

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
    a.start_sip(register=False)
    a.fake_sock = sock
    a.fake_threads = started
    return a


def respond(request, status, extra=(), via=None, cseq=None, call_id=None):
    """Build a SIP response by copying the request's Via/CSeq/Call-ID (and the
    remaining headers); override any of them via keyword arguments."""
    head, sep, body = request.partition('\r\n\r\n')
    out = [status]
    for line in head.split('\r\n')[1:]:
        low = line.lower()
        if via is not None and low.startswith('via:'):
            line = 'Via: ' + via
        elif cseq is not None and low.startswith('cseq:'):
            line = 'CSeq: ' + cseq
        elif call_id is not None and low.startswith('call-id:'):
            line = 'Call-ID: ' + call_id
        out.append(line)
    out.extend(extra)
    return '\r\n'.join(out) + sep + body


def test_start_sip_without_register_initializes_state_and_skips_registrar(ata):
    assert ata.fake_sock.bind.call_args.args == (('127.0.0.1', 5062),)
    assert ata.reg_ok is False
    assert ata.reg_nonce is None
    assert ata.reg_event.is_set() is False
    # register=False: only the sip listener thread, never the registrar.
    assert len(ata.fake_threads) == 1
    assert ata.fake_threads[0].target == ata.sip_listener
    assert all(t.started for t in ata.fake_threads)


def test_registration_request_shape_and_cseq_increments(ata):
    req1 = ata.registration_request(expires=300)
    req2 = ata.registration_request(expires=120)
    cseq1 = sip_header(req1, 'CSeq')
    cseq2 = sip_header(req2, 'CSeq')
    assert re.fullmatch(r'\d+ REGISTER', cseq1)
    assert int(cseq2.split()[0]) == int(cseq1.split()[0]) + 1
    assert sip_header(req1, 'Call-ID') == sip_header(req2, 'Call-ID')
    assert 'branch=z9hG4bK' in sip_header(req1, 'Via')
    assert 'expires=300' in req1
    assert 'expires=120' in req2
    assert 'Authorization' not in req1  # no challenge seen yet


def test_wrong_branch_response_is_ignored(ata):
    req = ata.registration_request()
    via = sip_header(req, 'Via')
    wrong = re.sub(r'branch=z9hG4bK[0-9a-f]+', 'branch=z9hG4bK0000000000', via)
    assert wrong != via
    ata.handle_sip_message(respond(req, 'SIP/2.0 200 OK', via=wrong), ADDR)
    assert ata.reg_ok is False
    assert ata.reg_nonce is None
    assert ata.reg_event.is_set() is False


def test_superseded_cseq_response_is_ignored(ata):
    req = ata.registration_request()
    current = int(sip_header(req, 'CSeq').split()[0])
    stale = f'{current - 1} REGISTER'
    ata.handle_sip_message(respond(req, 'SIP/2.0 200 OK', cseq=stale), ADDR)
    assert ata.reg_ok is False
    assert ata.reg_nonce is None
    assert ata.reg_event.is_set() is False


@pytest.mark.parametrize('status', ['SIP/2.0 100 Trying', 'SIP/2.0 180 Ringing',
                                    'SIP/2.0 183 Session Progress'])
def test_provisional_response_is_not_terminal(ata, status):
    req = ata.registration_request()
    ata.handle_sip_message(respond(req, status), ADDR)
    assert ata.reg_ok is False
    assert ata.reg_nonce is None
    assert ata.reg_event.is_set() is False


def test_matching_401_challenge_stored_and_answered_with_authorization(ata):
    req = ata.registration_request()
    challenge = 'Digest realm="example.invalid", nonce="www-nonce-1", qop="auth"'
    ata.handle_sip_message(respond(req, 'SIP/2.0 401 Unauthorized',
                                   extra=[f'WWW-Authenticate: {challenge}']), ADDR)
    # NB: the implementation's challenge regex keeps the trailing \r; harmless
    # for parse_digest (\s matches \r), so compare stripped.
    assert ata.reg_nonce.strip() == challenge
    assert ata.reg_event.is_set() is True
    assert ata.reg_ok is False
    req2 = ata.registration_request()
    assert 'Authorization: Digest' in req2
    assert 'nonce="www-nonce-1"' in req2


def test_matching_200_ok_completes_registration(ata):
    req = ata.registration_request()
    ata.handle_sip_message(respond(req, 'SIP/2.0 200 OK'), ADDR)
    assert ata.reg_ok is True
    assert ata.reg_nonce is None
    assert ata.reg_event.is_set() is True


def test_response_via_decorations_accepted(ata):
    req = ata.registration_request()
    via = sip_header(req, 'Via') + ';received=192.0.2.9;rport=5060'
    ata.handle_sip_message(respond(req, 'SIP/2.0 200 OK', via=via), ADDR)
    assert ata.reg_ok is True
    assert ata.reg_nonce is None
    assert ata.reg_event.is_set() is True


def test_407_challenge_leads_to_proxy_authorization_in_next_request(ata):
    req = ata.registration_request()
    challenge = 'Digest realm="example.invalid", nonce="proxy-nonce-1"'
    ata.handle_sip_message(respond(req, 'SIP/2.0 407 Proxy Authentication Required',
                                   extra=[f'Proxy-Authenticate: {challenge}']), ADDR)
    assert ata.reg_nonce.strip() == challenge
    assert ata.reg_event.is_set() is True
    assert ata.reg_ok is False
    req2 = ata.registration_request()
    assert 'Proxy-Authorization: Digest' in req2
    assert 'nonce="proxy-nonce-1"' in req2


def test_duplicate_terminal_response_does_not_alter_completed_transaction(ata):
    req = ata.registration_request()
    resp = respond(req, 'SIP/2.0 200 OK')
    ata.handle_sip_message(resp, ADDR)
    assert ata.reg_ok is True
    assert ata.reg_nonce is None
    assert ata.reg_event.is_set() is True
    # retransmissions of the same terminal response must be idempotent
    ata.handle_sip_message(resp, ADDR)
    ata.handle_sip_message(resp, ADDR)
    assert ata.reg_ok is True
    assert ata.reg_nonce is None


# ------------------------------------------------ TASK-0015 P1/P2 reproductions
CHALLENGE = 'Digest realm="example.invalid", nonce="n-1", qop="auth"'


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


def test_provisional_response_with_cached_nonce_is_timeout_not_challenge(ata):
    """P1: a 180 to an authenticated REGISTER must not yield 'challenge'."""
    ata.reg_event = FakeEvent()          # never set: full bounded schedule runs
    ata.reg_nonce = CHALLENGE            # cached auth from an earlier challenge
    req = ata.registration_request(300)
    assert b'Authorization: Digest' in req.encode()
    ata.handle_sip_message(respond(req, 'SIP/2.0 180 Ringing'), ADDR)
    outcome = ata._register_transaction(req.encode())
    assert outcome == 'timeout'          # not 'challenge'
    assert ata.reg_nonce == CHALLENGE    # cached auth preserved for retry
    assert ata.reg_ok is False and ata.reg_failed is False


def test_401_with_wrong_scheme_header_is_bounded_failure_not_challenge(ata):
    """P2: 401 must use WWW-Authenticate; a Proxy-Authenticate there is unusable."""
    ata.reg_event = FakeEvent()
    ata.reg_nonce = CHALLENGE
    req = ata.registration_request(300)
    ata.handle_sip_message(respond(req, 'SIP/2.0 401 Unauthorized',
                                   extra=['Proxy-Authenticate: Digest realm="r", nonce="px"']), ADDR)
    outcome = ata._register_transaction(req.encode())
    assert outcome == 'failed'           # bounded terminal failure, not 'challenge'
    assert ata.reg_nonce == CHALLENGE    # wrong-scheme header must not overwrite it
    req2 = ata.registration_request(300)
    assert 'Proxy-Authorization' not in req2


def test_407_with_wrong_scheme_header_is_bounded_failure_not_challenge(ata):
    """P2: 407 must use Proxy-Authenticate; a WWW-Authenticate there is unusable."""
    ata.reg_event = FakeEvent()
    ata.reg_nonce = CHALLENGE
    req = ata.registration_request(300)
    ata.handle_sip_message(respond(req, 'SIP/2.0 407 Proxy Authentication Required',
                                   extra=['WWW-Authenticate: Digest realm="r", nonce="wx"']), ADDR)
    outcome = ata._register_transaction(req.encode())
    assert outcome == 'failed'
    assert ata.reg_nonce == CHALLENGE
    req2 = ata.registration_request(300)
    assert 'Proxy-Authorization' not in req2


def test_401_without_authenticate_header_is_bounded_failure(ata):
    ata.reg_event = FakeEvent()
    req = ata.registration_request(300)
    ata.handle_sip_message(respond(req, 'SIP/2.0 401 Unauthorized'), ADDR)
    assert ata.reg_event.is_set() is True   # transaction ends promptly
    outcome = ata._register_transaction(req.encode())
    assert outcome == 'failed'
    assert ata.reg_nonce is None


def test_401_www_authenticate_is_challenge_with_authorization(ata):
    ata.reg_event = FakeEvent()
    req = ata.registration_request(300)
    ata.handle_sip_message(respond(req, 'SIP/2.0 401 Unauthorized',
                                   extra=[f'WWW-Authenticate: {CHALLENGE}']), ADDR)
    outcome = ata._register_transaction(req.encode())
    assert outcome == 'challenge'
    req2 = ata.registration_request(300)
    assert 'Authorization: Digest' in req2
    assert 'Proxy-Authorization' not in req2


def test_407_proxy_authenticate_is_challenge_with_proxy_authorization(ata):
    ata.reg_event = FakeEvent()
    req = ata.registration_request(300)
    ata.handle_sip_message(respond(req, 'SIP/2.0 407 Proxy Authentication Required',
                                   extra=['Proxy-Authenticate: Digest realm="r", nonce="p1"']), ADDR)
    outcome = ata._register_transaction(req.encode())
    assert outcome == 'challenge'
    req2 = ata.registration_request(300)
    assert 'Proxy-Authorization: Digest' in req2


def test_challenge_then_provisional_same_transaction_still_challenge(ata):
    ata.reg_event = FakeEvent()
    req = ata.registration_request(300)
    ata.handle_sip_message(respond(req, 'SIP/2.0 401 Unauthorized',
                                   extra=[f'WWW-Authenticate: {CHALLENGE}']), ADDR)
    ata.handle_sip_message(respond(req, 'SIP/2.0 180 Ringing'), ADDR)
    assert ata._register_transaction(req.encode()) == 'challenge'


def test_provisional_then_challenge_same_transaction_is_challenge(ata):
    ata.reg_event = FakeEvent()
    req = ata.registration_request(300)
    ata.handle_sip_message(respond(req, 'SIP/2.0 180 Ringing'), ADDR)
    ata.handle_sip_message(respond(req, 'SIP/2.0 401 Unauthorized',
                                   extra=[f'WWW-Authenticate: {CHALLENGE}']), ADDR)
    assert ata._register_transaction(req.encode()) == 'challenge'


def test_200_without_reason_phrase_is_recognized(ata):
    """Status parse uses the validated regex group, not a text split."""
    req = ata.registration_request(300)
    ata.handle_sip_message(respond(req, 'SIP/2.0 200'), ADDR)  # no reason phrase
    assert ata.reg_ok is True
    assert ata.reg_event.is_set() is True


def test_status_line_with_four_digit_code_is_ignored(ata):
    """Bounded: the status regex still rejects non-3-digit codes."""
    req = ata.registration_request(300)
    ata.handle_sip_message(respond(req, 'SIP/2.0 2000 Weird'), ADDR)
    assert ata.reg_ok is False
    assert ata.reg_event.is_set() is False
