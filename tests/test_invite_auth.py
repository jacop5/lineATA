"""TASK-0040 outgoing INVITE auth/retry correlation audit (mock UDP queue only).

Drives the REAL SIPCall.invite()/handle_responses request builders and real
parse_digest against pre-queued responses on call.responses — no permissive
mocks, no real sockets/network. Captures exact wire methods, CSeq, branch and
auth headers. Positive constraints (CANCEL->late200 ACK/BYE, per-method dedup)
are covered elsewhere and not re-investigated here.
"""
import re
import threading
from unittest.mock import Mock

import pytest

from bridge.ata import LinearATA, SIPCall, sdp_local, sip_header

CFG = {'login': 'test', 'password': 'test-only', 'domain': 'example.invalid',
       'pbx_ip': '127.0.0.1', 'pbx_port': 5060, 'local_ip': '127.0.0.1',
       'sip_port': 5062}
ADDR = ('127.0.0.1', 5060)
CHALLENGE_A = 'Digest realm="example.invalid", nonce="nonce-A", qop="auth"'
CHALLENGE_B = 'Digest realm="example.invalid", nonce="nonce-B", stale=true, qop="auth"'
CHALLENGE_P = 'Digest realm="example.invalid", nonce="nonce-P", qop="auth"'


@pytest.fixture
def ata(monkeypatch):
    class FakeThread:
        def __init__(self, *a, **k):
            self.target = k.get('target')

        def start(self):
            pass

        def join(self, timeout=None):
            pass

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
    yield a
    a.stop.set()


def new_call(ata):
    call = SIPCall(ata, '123')
    call.sock = ata.fake_sock
    call.remote = ADDR
    ata.call = call
    ata.invite_transactions[call.call_id] = call
    return call


def sent_all(ata):
    out = []
    for c in ata.fake_sock.sendto.call_args_list:
        text = c.args[0].decode()
        if text.startswith('SIP/2.0'):
            out.append(('resp', text.split(' ', 2)[1]))
        else:
            out.append((text.split(' ', 1)[0], text))
    return out


def respond_to(request, status, extra=(), body=''):
    """Build a response copying ONLY Via/From/To/Call-ID/CSeq from the request
    (To gains a tag), then the actual extra headers, exactly one callee
    Contact, and a correct Content-Length/body. The request's own Contact,
    Authorization or Proxy-Authorization are never echoed as response headers."""
    req = request.decode() if isinstance(request, bytes) else request
    head = req.split('\r\n\r\n', 1)[0]
    keep = {}
    for line in head.split('\r\n')[1:]:
        name = line.split(':', 1)[0].strip().lower()
        if name in ('via', 'from', 'to', 'call-id', 'cseq'):
            keep[name] = line
    to = keep['to']
    if 'tag=' not in to.lower():
        to += ';tag=remote1'
    out = [status, keep['via'], keep['from'], to, keep['call-id'], keep['cseq']]
    out.extend(extra)
    out.append('Contact: <sip:callee@127.0.0.1:5060>')
    if body:
        out.append('Content-Type: application/sdp')
    out.append(f'Content-Length: {len(body.encode())}')
    return '\r\n'.join(out) + '\r\n\r\n' + body


def feed(call, request, status, extra=(), body=''):
    call.responses.put((respond_to(request, status, extra, body), ADDR))


def spy_send(call, handler, max_sends=6):
    """Wrap call.send: record the wire frame, run the scenario handler, and if
    the scenario never yields a terminal response, deterministically feed a
    486 on a late INVITE so handle_responses exits instead of retransmitting
    for the real 45 s timeout."""
    orig_send = call.send
    state = {'n': 0}

    def send_spy(data, addr):
        orig_send(data, addr)
        state['n'] += 1
        req = data.decode() if isinstance(data, bytes) else data
        if state['n'] > max_sends and req.startswith('INVITE'):
            feed(call, data, 'SIP/2.0 486 Busy Here')
            return
        handler(req, data)

    return send_spy


def test_single_401_then_authenticated_200(ata):
    call = new_call(ata)

    def handler(req, data):
        if req.startswith('INVITE') and not sip_header(req, 'Authorization') \
                and not sip_header(req, 'Proxy-Authorization') and call.responses.empty():
            feed(call, data, 'SIP/2.0 401 Unauthorized',
                 extra=[f'WWW-Authenticate: {CHALLENGE_A}'])
        elif req.startswith('INVITE') and sip_header(req, 'Authorization'):
            feed(call, data, 'SIP/2.0 200 OK', body=sdp_local('127.0.0.1', 24000))

    call.send = spy_send(call, handler)
    call.start_media = Mock()
    ok = call.invite()
    try:
        assert ok is True and call.established is True
        reqs = [t for m, t in sent_all(ata) if m == 'INVITE']
        assert len(reqs) == 2
        assert sip_header(reqs[0], 'CSeq') == '1 INVITE'
        assert sip_header(reqs[1], 'CSeq') == '2 INVITE'
        assert sip_header(reqs[0], 'Authorization') == ''
        assert sip_header(reqs[1], 'Authorization').startswith('Digest')
        assert 'nonce="nonce-A"' in sip_header(reqs[1], 'Authorization')
        # distinct branch per transaction, same Call-ID
        assert sip_header(reqs[0], 'Via') != sip_header(reqs[1], 'Via')
        assert sip_header(reqs[0], 'Call-ID') == sip_header(reqs[1], 'Call-ID') == call.call_id
        # every challenge/answer ACKed with the matching CSeq
        acks = [t for m, t in sent_all(ata) if m == 'ACK']
        assert [sip_header(a, 'CSeq') for a in acks] == ['1 ACK', '2 ACK']
    finally:
        call.close()


def test_fresh_stale_401_after_first_auth_is_acked_but_not_reauthed(ata):
    """A fresh stale=true 401 after the first authenticated retry: ACKed, then
    the worker stops (auth is not None) — no second auth attempt. Confirmed
    bounded behavior; whether a fresh stale=true SHOULD trigger re-auth is a
    compatibility hypothesis, not asserted from RFC memory."""
    call = new_call(ata)
    fed = []

    def handler(req, data):
        if req.startswith('INVITE') and not fed:
            fed.append(1)
            feed(call, data, 'SIP/2.0 401 Unauthorized',
                 extra=[f'WWW-Authenticate: {CHALLENGE_A}'])
        elif req.startswith('INVITE') and sip_header(req, 'Authorization') and len(fed) == 1:
            fed.append(2)
            feed(call, data, 'SIP/2.0 401 Unauthorized',
                 extra=[f'WWW-Authenticate: {CHALLENGE_B}'])   # fresh, stale=true

    call.send = spy_send(call, handler)
    call.start_media = Mock()
    ok = call.invite()
    try:
        assert ok is False
        reqs = [t for m, t in sent_all(ata) if m == 'INVITE']
        assert len(reqs) == 2                      # exactly one auth retry, bounded
        assert 'nonce="nonce-A"' in sip_header(reqs[1], 'Authorization')
        assert 'nonce-B' not in sip_header(reqs[1], 'Authorization')
        acks = [t for m, t in sent_all(ata) if m == 'ACK']
        assert [sip_header(a, 'CSeq') for a in acks] == ['1 ACK', '2 ACK']  # both 401s ACKed
    finally:
        call.close()


def test_persistent_401_bad_credentials_is_bounded_single_retry(ata):
    call = new_call(ata)
    n = [0]

    def handler(req, data):
        if req.startswith('INVITE'):
            n[0] += 1
            feed(call, data, 'SIP/2.0 401 Unauthorized',
                 extra=[f'WWW-Authenticate: {CHALLENGE_A}'])

    call.send = spy_send(call, handler)
    call.start_media = Mock()
    ok = call.invite()
    try:
        assert ok is False
        assert n[0] == 2                           # initial + exactly one retry
        assert not call.established
    finally:
        call.close()


def test_407_then_401_does_not_attempt_www_auth(ata):
    """407 builds Proxy-Authorization; a following 401 is ACKed but not
    re-authed (single auth leg). No proxy-routing claim — direct-PBX scope.
    Exact header names are used because 'Proxy-Authorization' contains the
    substring 'Authorization'."""
    call = new_call(ata)
    fed = []

    def handler(req, data):
        if req.startswith('INVITE') and not fed:
            fed.append(1)
            feed(call, data, 'SIP/2.0 407 Proxy Authentication Required',
                 extra=[f'Proxy-Authenticate: {CHALLENGE_P}'])
        elif req.startswith('INVITE') and sip_header(req, 'Proxy-Authorization') and len(fed) == 1:
            fed.append(2)
            feed(call, data, 'SIP/2.0 401 Unauthorized',
                 extra=[f'WWW-Authenticate: {CHALLENGE_A}'])

    call.send = spy_send(call, handler)
    call.start_media = Mock()
    ok = call.invite()
    try:
        assert ok is False
        reqs = [t for m, t in sent_all(ata) if m == 'INVITE']
        assert len(reqs) == 2
        assert sip_header(reqs[1], 'Proxy-Authorization').startswith('Digest')
        assert sip_header(reqs[1], 'Authorization') == ''   # no WWW leg after 407
        acks = [t for m, t in sent_all(ata) if m == 'ACK']
        assert [sip_header(a, 'CSeq') for a in acks] == ['1 ACK', '2 ACK']
    finally:
        call.close()


def test_wrong_cseq_challenge_is_ignored_not_acked(ata):
    """A 401 carrying an obsolete CSeq is not for this transaction: ignored
    without ACK; the correct-CSeq final (486) is ACKed and ends the call."""
    call = new_call(ata)

    def handler(req, data):
        if req.startswith('INVITE') and not sip_header(req, 'Authorization') \
                and call.responses.empty():
            # obsolete CSeq 99 challenge, then the real final for CSeq 1:
            bad = respond_to(data, 'SIP/2.0 401 Unauthorized',
                             extra=[f'WWW-Authenticate: {CHALLENGE_A}'])
            bad = re.sub(r'^CSeq: \d+ INVITE', 'CSeq: 99 INVITE', bad, count=1, flags=re.M)
            call.responses.put((bad, ADDR))
            feed(call, data, 'SIP/2.0 486 Busy Here')

    call.send = spy_send(call, handler)
    call.start_media = Mock()
    ok = call.invite()
    try:
        assert ok is False
        wire = sent_all(ata)
        acks = [t for m, t in wire if m == 'ACK']
        assert len(acks) == 1 and sip_header(acks[0], 'CSeq') == '1 ACK'  # only the 486 ACKed
        assert len([1 for m, t in wire if m == 'INVITE']) == 1            # no re-INVITE for wrong CSeq
    finally:
        call.close()
