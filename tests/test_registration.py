"""Offline REGISTER reliability tests: mocked socket, fake events, no network.

Covers the registration-only fixes in bridge/ata.py: granted-expiry honoring
(our Contact, Expires fallback), bounded 423 Min-Expires, bounded interruptible
challenge backoff without stopping the daemon, identical-byte retransmission
at 0.5/1/2 s, and rejection of wrong-branch / obsolete-CSeq responses.

Run against e0f3727 these tests fail (bug reproduction); after the fix they pass.
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
CHALLENGE = 'Digest realm="example.invalid", nonce="n-1", qop="auth"'
OUR_CONTACT = 'test@127.0.0.1:5062'


class FakeEvent:
    def __init__(self):
        self._set = False
        self.waits = []
        self.on_wait = None  # optional callback(waits) after each wait

    def set(self):
        self._set = True

    def clear(self):
        self._set = False

    def is_set(self):
        return self._set

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.on_wait:
            self.on_wait(self.waits)
        return self._set


class FakeStop:
    def __init__(self):
        self._set = False
        self.waits = []
        self.set_calls = 0

    def set(self):
        self.set_calls += 1
        self._set = True

    def is_set(self):
        return self._set

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self._set


@pytest.fixture
def ata(monkeypatch):
    """LinearATA after start_sip(register=False) on a mocked socket; no real
    socket, listener or registrar thread is created."""
    class FakeThread:
        def __init__(self, *a, **k):
            self.target = k.get('target')

        def start(self):
            pass

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
    return a


def response_for(data, status, extra=(), via=None, cseq=None, call_id=None):
    """Build a SIP response by copying a request's Via/CSeq/Call-ID."""
    req = data.decode() if isinstance(data, bytes) else data
    head, sep, body = req.partition('\r\n\r\n')
    out = [status]
    for line in head.split('\r\n')[1:]:
        low = line.lower()
        if low.startswith('contact:'):
            continue  # a response carries its own Contact, not the request's
        if via is not None and low.startswith('via:'):
            line = 'Via: ' + via
        elif cseq is not None and low.startswith('cseq:'):
            line = 'CSeq: ' + cseq
        elif call_id is not None and low.startswith('call-id:'):
            line = 'Call-ID: ' + call_id
        out.append(line)
    out.extend(extra)
    return '\r\n'.join(out) + sep + body


# ---------------------------------------------------------------- expiry
def test_granted_expires_read_from_our_contact(ata):
    req = ata.registration_request(300)
    ata.handle_sip_message(response_for(
        req, 'SIP/2.0 200 OK',
        extra=[f'Contact: <sip:{OUR_CONTACT}>;expires=60']), ADDR)
    assert ata.reg_ok is True
    assert ata.reg_event.is_set() is True
    assert ata.reg_granted == 60


def test_granted_expires_falls_back_to_expires_header(ata):
    req = ata.registration_request(300)
    ata.handle_sip_message(response_for(
        req, 'SIP/2.0 200 OK',
        extra=[f'Contact: <sip:{OUR_CONTACT}>', 'Expires: 90']), ADDR)
    assert ata.reg_granted == 90


def test_granted_expires_ignores_foreign_contact(ata):
    req = ata.registration_request(300)
    ata.handle_sip_message(response_for(
        req, 'SIP/2.0 200 OK',
        extra=['Contact: <sip:other@192.0.2.9>;expires=5', 'Expires: 45']), ADDR)
    assert ata.reg_granted == 45


def test_granted_expires_clamped_to_requested(ata):
    req = ata.registration_request(300)
    ata.handle_sip_message(response_for(
        req, 'SIP/2.0 200 OK',
        extra=[f'Contact: <sip:{OUR_CONTACT}>;expires=9999']), ADDR)
    assert ata.reg_granted == 300


def test_renewal_delay_is_strictly_before_any_positive_lease():
    d = LinearATA._reg_renewal_delay
    assert d(300) == 240.0          # preserves the old 300 s schedule
    assert d(60) == 30.0
    for lease in (1, 2, 5, 20, 60, 119, 120, 300, 3600):
        assert 0 < d(lease) < lease, lease


# ---------------------------------------------------------------- 423
def test_423_min_expires_is_bounded(ata):
    req = ata.registration_request(300)
    ata.handle_sip_message(response_for(
        req, 'SIP/2.0 423 Interval Too Brief',
        extra=['Min-Expires: 99999999']), ADDR)
    assert ata.reg_min_expires == 86400
    assert ata.reg_event.is_set() is True
    assert ata.reg_ok is False


def test_registrar_applies_bounded_min_expires_to_next_register(ata):
    ata.reg_event = FakeEvent()
    stop = FakeStop()
    ata.stop = stop
    sent = []

    def sendto(data, addr):
        sent.append(bytes(data))
        if len(sent) == 1:
            ata.handle_sip_message(response_for(
                data, 'SIP/2.0 423 Interval Too Brief',
                extra=['Min-Expires: 99999999']), addr)
        else:
            ata.handle_sip_message(response_for(data, 'SIP/2.0 200 OK'), addr)
            stop._set = True

    ata.fake_sock.sendto.side_effect = sendto
    ata.registrar()
    assert len(sent) == 2
    assert b'expires=300' in sent[0]
    assert b'expires=86400' in sent[1]   # bounded 423 adjustment
    assert stop.set_calls == 0


# ---------------------------------------------------------------- retransmission
def test_transaction_retransmits_identical_bytes_at_half_one_two_seconds(ata):
    ata.reg_event = FakeEvent()  # never set: pure timeout path
    req = ata.registration_request(300)
    data = req.encode()
    outcome = ata._register_transaction(data)
    sent = [c.args[0] for c in ata.fake_sock.sendto.call_args_list]
    assert len(sent) == 4                       # initial + 3 retransmissions
    assert all(s == data for s in sent)         # identical bytes/branch/CSeq
    # retransmit schedule plus the bounded final response window (TASK-0008)
    assert ata.reg_event.waits == [0.5, 1.0, 2.0, 0.5]
    assert outcome == 'timeout'


def test_registrar_timeout_path_is_bounded(ata):
    ata.reg_event = FakeEvent()
    stop = FakeStop()
    ata.stop = stop
    sent = []

    def sendto(data, addr):
        sent.append(bytes(data))

    # stop is requested during the final response window, after the last send
    def _stop_after_final_window(waits):
        if len(waits) == 4:
            stop._set = True
    ata.reg_event.on_wait = _stop_after_final_window
    ata.fake_sock.sendto.side_effect = sendto
    ata.registrar()
    assert len(sent) == 4 and len(set(sent)) == 1
    assert ata.reg_event.waits == [0.5, 1.0, 2.0, 0.5]  # + final response window
    assert stop.waits == [10]                   # single bounded retry pause
    assert stop.set_calls == 0


# ---------------------------------------------------------------- challenges
def test_registrar_repeated_challenges_backoff_bounded_and_never_stops_daemon(ata):
    ata.reg_event = FakeEvent()
    stop = FakeStop()
    ata.stop = stop
    sent = []

    def sendto(data, addr):
        sent.append(bytes(data))
        ata.handle_sip_message(response_for(
            data, 'SIP/2.0 401 Unauthorized',
            extra=[f'WWW-Authenticate: {CHALLENGE}']), addr)
        if len(sent) >= 6:
            stop._set = True

    ata.fake_sock.sendto.side_effect = sendto
    ata.registrar()
    assert len(sent) == 6
    assert stop.set_calls == 0                  # auth problems never stop the daemon
    assert stop.waits == [5.0, 10.0, 20.0, 40.0]  # interruptible exponential backoff
    assert b'Authorization: Digest' in sent[1]  # challenged request is re-sent with auth
    cseqs = [int(sip_header(s.decode(), 'CSeq').split()[0]) for s in sent]
    assert cseqs == sorted(cseqs) and len(set(cseqs)) == len(cseqs)
    assert len({sip_header(s.decode(), 'Call-ID') for s in sent}) == 1


def test_registrar_auth_error_does_not_stop_daemon(ata):
    ata.reg_event = FakeEvent()
    stop = FakeStop()
    ata.stop = stop
    # Unsupported algorithm: parse_digest raises ValueError inside registration_request.
    ata.reg_nonce = 'Digest realm="r", nonce="n", algorithm=SHA-256'
    sent = []

    def sendto(data, addr):
        sent.append(bytes(data))
        ata.handle_sip_message(response_for(
            data, 'SIP/2.0 401 Unauthorized',
            extra=['WWW-Authenticate: Digest realm="r", nonce="n2", algorithm=SHA-256']),
            addr)
        if len(sent) >= 2:
            stop._set = True

    ata.fake_sock.sendto.side_effect = sendto
    ata.registrar()
    assert stop.set_calls == 0                  # e0f3727 called self.stop.set() here
    assert sent                               # the registrar kept trying, bounded


def test_registrar_honors_short_grant_and_renews_before_lease_ends(ata):
    ata.reg_event = FakeEvent()
    stop = FakeStop()
    ata.stop = stop
    sent = []

    def sendto(data, addr):
        sent.append(bytes(data))
        if len(sent) == 1:
            ata.handle_sip_message(response_for(
                data, 'SIP/2.0 200 OK',
                extra=[f'Contact: <sip:{OUR_CONTACT}>;expires=20']), addr)
            stop._set = True  # end the loop once the renewal wait is recorded

    ata.fake_sock.sendto.side_effect = sendto
    ata.registrar()
    assert ata.reg_granted == 20
    assert len(stop.waits) == 1
    assert 0 < stop.waits[0] < 20               # strictly before the short lease
    assert ata.reg_event.waits == [0.5]         # no retransmission after the 200
    assert stop.set_calls == 0


# ---------------------------------------------------------------- correlation
def test_wrong_branch_and_stale_cseq_and_foreign_call_id_are_ignored(ata):
    req = ata.registration_request()
    via = sip_header(req, 'Via')
    wrong_via = re.sub(r'branch=z9hG4bK[0-9a-f]+', 'branch=z9hG4bKdeadbeef00', via)
    assert wrong_via != via
    current = sip_header(req, 'CSeq').split()[0]
    ata.handle_sip_message(response_for(req, 'SIP/2.0 401 Unauthorized',
                                         extra=[f'WWW-Authenticate: {CHALLENGE}'],
                                         via=wrong_via), ADDR)
    ata.handle_sip_message(response_for(req, 'SIP/2.0 200 OK',
                                         cseq=f'{int(current) - 1} REGISTER'), ADDR)
    ata.handle_sip_message(response_for(req, 'SIP/2.0 200 OK', call_id='other'), ADDR)
    assert ata.reg_ok is False
    assert ata.reg_nonce is None
    assert ata.reg_event.is_set() is False


def test_superseded_challenge_does_not_overwrite_current_nonce(ata):
    req1 = ata.registration_request()
    ata.handle_sip_message(response_for(
        req1, 'SIP/2.0 401 Unauthorized',
        extra=['WWW-Authenticate: Digest realm="r", nonce="old", qop="auth"']), ADDR)
    assert ata.reg_nonce.strip().endswith('nonce="old", qop="auth"')
    req2 = ata.registration_request()  # advances CSeq and branch
    ata.handle_sip_message(response_for(
        req2, 'SIP/2.0 401 Unauthorized',
        extra=['WWW-Authenticate: Digest realm="r", nonce="new", qop="auth"']), ADDR)
    # late retransmission of the challenge for the superseded CSeq:
    ata.handle_sip_message(response_for(
        req1, 'SIP/2.0 401 Unauthorized',
        extra=['WWW-Authenticate: Digest realm="r", nonce="old", qop="auth"']), ADDR)
    assert 'nonce="new"' in ata.reg_nonce


def test_provisional_register_responses_are_not_terminal(ata):
    req = ata.registration_request()
    for status in ('SIP/2.0 100 Trying', 'SIP/2.0 180 Ringing'):
        ata.handle_sip_message(response_for(req, status), ADDR)
    assert ata.reg_ok is False and ata.reg_nonce is None
    assert ata.reg_event.is_set() is False


# ---------------------------------------------------------------- lead counterexamples (TASK-0008)
def test_multi_contact_header_uses_our_uris_own_expires(ata):
    """Lead counterexample multi_contact_lease: expected 60, observed 5."""
    req = ata.registration_request(300)
    ata.handle_sip_message(response_for(
        req, 'SIP/2.0 200 OK',
        extra=['Contact: <sip:other@192.0.2.9>;expires=5, '
               f'<sip:{OUR_CONTACT}>;expires=60']), ADDR)
    assert ata.reg_granted == 60


def test_multiple_contact_lines_use_our_uris_own_expires(ata):
    req = ata.registration_request(300)
    ata.handle_sip_message(response_for(
        req, 'SIP/2.0 200 OK',
        extra=['Contact: <sip:other@192.0.2.9>;expires=5',
               f'Contact: <sip:{OUR_CONTACT}>;expires=42']), ADDR)
    assert ata.reg_granted == 42


def test_contact_uri_is_matched_exactly_not_by_substring(ata):
    req = ata.registration_request(300)
    # lookalike URIs must not match our Contact:
    ata.handle_sip_message(response_for(
        req, 'SIP/2.0 200 OK',
        extra=['Contact: <sip:xtest@127.0.0.1:5062>;expires=5',
               'Contact: <sip:test@127.0.0.1:50621>;expires=7',
               'Expires: 33']), ADDR)
    assert ata.reg_granted == 33  # only the Expires fallback, not 5 or 7


def test_explicit_expires_zero_is_kept_and_never_reported_positive(ata):
    ata.reg_event = FakeEvent()
    stop = FakeStop()
    ata.stop = stop
    sent = []

    def sendto(data, addr):
        sent.append(bytes(data))
        if len(sent) == 1:
            ata.handle_sip_message(response_for(
                data, 'SIP/2.0 200 OK',
                extra=[f'Contact: <sip:{OUR_CONTACT}>;expires=0']), addr)
            stop._set = True

    ata.fake_sock.sendto.side_effect = sendto
    ata.registrar()
    assert ata.reg_granted == 0            # explicit zero, not None, not positive
    assert stop.waits[0] == 0.5            # renewal delay for a zero lease


def test_missing_expires_on_our_contact_is_none_not_zero(ata):
    req = ata.registration_request(300)
    ata.handle_sip_message(response_for(
        req, 'SIP/2.0 200 OK', extra=[f'Contact: <sip:{OUR_CONTACT}>']), ADDR)
    assert ata.reg_granted is None         # missing -> Expires/request fallback


def test_lost_authenticated_transaction_is_timeout_not_challenge(ata):
    """Lead counterexample lost_authenticated_transaction: expected timeout,
    observed challenge (cached nonce misread as a new challenge)."""
    ata.reg_event = FakeEvent()  # never set: no response at all
    ata.reg_nonce = CHALLENGE    # valid cached auth from an earlier challenge
    req = ata.registration_request(300)
    assert b'Authorization: Digest' in req.encode()
    outcome = ata._register_transaction(req.encode())
    assert outcome == 'timeout'
    assert ata.reg_nonce == CHALLENGE      # cached auth preserved for retry


def test_delayed_challenge_after_completed_200_cannot_overwrite_completion(ata):
    req = ata.registration_request()
    ata.handle_sip_message(response_for(req, 'SIP/2.0 200 OK'), ADDR)
    assert ata.reg_ok is True and ata.reg_nonce is None
    # delayed/duplicate 401 for the same (already settled) transaction:
    ata.handle_sip_message(response_for(
        req, 'SIP/2.0 401 Unauthorized',
        extra=[f'WWW-Authenticate: {CHALLENGE}']), ADDR)
    assert ata.reg_ok is True              # completion not overwritten
    assert ata.reg_nonce is None           # stale challenge not accepted


def test_backoff_is_bounded_before_exponentiation(ata):
    ata.stop = FakeStop()
    ata.reg_challenges = 5000              # long repeated failures
    assert ata._reg_challenge_backoff() is False
    assert ata.stop.waits == [60.0]        # capped, no OverflowError


def test_transaction_stops_without_sending_after_stop(ata):
    ata.reg_event = FakeEvent()
    stop = FakeStop()
    ata.stop = stop
    sent = []

    def sendto(data, addr):
        sent.append(bytes(data))
        stop._set = True                   # stop arrives after the first send

    ata.fake_sock.sendto.side_effect = sendto
    outcome = ata._register_transaction(ata.registration_request(300).encode())
    assert outcome == 'stopped'
    assert len(sent) == 1                  # no retransmission after stop


def test_final_retransmission_has_a_bounded_response_window(ata):
    ata.reg_event = FakeEvent()
    ata._register_transaction(ata.registration_request(300).encode())
    assert ata.reg_event.waits == [0.5, 1.0, 2.0, 0.5]


# ---------------------------------------------------------------- probe compatibility
def test_probe_style_register_and_deregister_flow(ata):
    """tools/sip_register_probe.py drives registration_request + reg_event +
    reg_ok/reg_nonce through start_sip(register=False); keep that working."""
    ata.reg_event = FakeEvent()
    # REGISTER with expires=60, challenged, answered:
    ata.reg_ok = False
    ata.reg_event.clear()
    req = ata.registration_request(60).encode()
    assert b'expires=60' in req
    ata.fake_sock.sendto(req, ADDR)
    ata.handle_sip_message(response_for(
        req, 'SIP/2.0 401 Unauthorized',
        extra=[f'WWW-Authenticate: {CHALLENGE}']), ADDR)
    assert ata.reg_event.wait(0.01) is True
    assert ata.reg_nonce is not None and ata.reg_ok is False
    req2 = ata.registration_request(60).encode()
    assert b'Authorization: Digest' in req2
    ata.fake_sock.sendto(req2, ADDR)
    ata.handle_sip_message(response_for(req2, 'SIP/2.0 200 OK'), ADDR)
    assert ata.reg_ok is True
    # DEREGISTER with expires=0:
    ata.reg_ok = False
    ata.reg_event.clear()
    req3 = ata.registration_request(0).encode()
    assert b'expires=0' in req3
    ata.fake_sock.sendto(req3, ADDR)
    ata.handle_sip_message(response_for(
        req3, 'SIP/2.0 200 OK',
        extra=[f'Contact: <sip:{OUR_CONTACT}>;expires=0']), ADDR)
    assert ata.reg_ok is True
