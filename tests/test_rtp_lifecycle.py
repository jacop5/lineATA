"""RTP resource-lifecycle regressions (TASK-0036).

Uses the REAL SIPCall.prepare_media/close — never a whole-method stub — with a
fake socket module whose bind() blocks on an Event barrier. Decisive evidence:
TASK-0033/artifacts/lead-actual-prepare-race.json (on frozen b8412e4…: candidate
opened but not closed, published after close). No real sockets/network; thread
joins bounded <=2s.
"""
import socket as real_socket
import threading
import types

import pytest

import bridge.ata as ata_mod
from bridge.ata import LinearATA, SIPCall

CFG = {'login': 't', 'password': 'x', 'domain': 'd', 'pbx_ip': '127.0.0.1',
       'pbx_port': 5060, 'local_ip': '127.0.0.1', 'sip_port': 5062, 'cold': True}

INVITE = ('INVITE sip:t@d SIP/2.0\r\nVia: v\r\nFrom: f;tag=a\r\nTo: t\r\n'
          'Call-ID: cid\r\nCSeq: 1 INVITE\r\n\r\n')


class FakeSocket:
    """Fake UDP socket; bind() can block on a barrier to model a slow OS bind."""
    instances = []
    bind_entered = None   # Event set when bind is entered
    bind_release = None   # Event that releases the blocked bind
    fail_bind = False

    def __init__(self, *a, **k):
        self.closed = False
        self.port = 41000 + len(FakeSocket.instances)
        FakeSocket.instances.append(self)

    def bind(self, addr):
        if FakeSocket.bind_entered is not None:
            FakeSocket.bind_entered.set()
        if FakeSocket.bind_release is not None:
            assert FakeSocket.bind_release.wait(2.0), "bind barrier timeout"
        if FakeSocket.fail_bind:
            raise OSError("bind failed (fake)")

    def settimeout(self, t):
        pass

    def getsockname(self):
        return ('127.0.0.1', self.port)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_socket_mod(monkeypatch):
    FakeSocket.instances = []
    FakeSocket.bind_entered = None
    FakeSocket.bind_release = None
    FakeSocket.fail_bind = False
    mod = types.SimpleNamespace(
        AF_INET=real_socket.AF_INET, SOCK_DGRAM=real_socket.SOCK_DGRAM,
        timeout=real_socket.timeout, socket=FakeSocket)
    monkeypatch.setattr(ata_mod, 'socket', mod)
    return FakeSocket


def make_ata():
    ata = LinearATA(dict(CFG))
    ata.log = lambda *a: None
    return ata


def test_blocked_bind_candidate_closed_and_never_published_after_close(fake_socket_mod, monkeypatch):
    # REAL prepare_media; bind parks on the barrier while close() runs.
    ata = make_ata()
    call = SIPCall(ata, 'incoming')
    fake_socket_mod.bind_entered = threading.Event()
    fake_socket_mod.bind_release = threading.Event()
    outcome = {}

    def worker():
        try:
            call.prepare_media()
            outcome['ret'] = 'ok'
        except OSError as exc:
            outcome['ret'] = repr(exc)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    assert fake_socket_mod.bind_entered.wait(2.0)
    candidate = fake_socket_mod.instances[-1]
    call.close()                       # close while candidate is unpublished
    assert call.closed
    fake_socket_mod.bind_release.set()
    t.join(2.0)
    assert not t.is_alive(), "prepare worker hung"
    # Desired: candidate closed and never attached to the closed call.
    assert candidate.closed, "candidate socket leaked open after close()"
    assert call.rtp is None, "candidate published onto a closed call"


def test_normal_prepare_advertises_bound_port(fake_socket_mod):
    ata = make_ata()
    call = SIPCall(ata, '69')
    assert call.prepare_media() is None or call.rtp is not None
    assert call.rtp is fake_socket_mod.instances[-1]
    assert call.local_rtp == call.rtp.getsockname()[1]


def test_repeat_prepare_does_not_allocate_again(fake_socket_mod):
    ata = make_ata()
    call = SIPCall(ata, '69')
    call.prepare_media()
    call.prepare_media()
    assert len(fake_socket_mod.instances) == 1


def test_bind_failure_closes_local_and_raises(fake_socket_mod):
    fake_socket_mod.fail_bind = True
    ata = make_ata()
    call = SIPCall(ata, '69')
    with pytest.raises(OSError):
        call.prepare_media()
    assert fake_socket_mod.instances[-1].closed
    assert call.rtp is None


def test_already_closed_call_allocates_nothing(fake_socket_mod):
    ata = make_ata()
    call = SIPCall(ata, '69')
    call.close()
    with pytest.raises(OSError, match='closed'):
        call.prepare_media()
    assert call.rtp is None
    assert fake_socket_mod.instances == []


def test_repeated_close_is_idempotent(fake_socket_mod):
    ata = make_ata()
    call = SIPCall(ata, '69')
    call.prepare_media()
    sock = call.rtp
    call.close()
    call.close()
    assert call.closed and sock.closed
    assert call.closed_event.is_set()


def test_simultaneous_preparations_publish_one_and_close_loser(fake_socket_mod):
    ata = make_ata()
    call = SIPCall(ata, '69')
    fake_socket_mod.bind_entered = threading.Event()
    fake_socket_mod.bind_release = threading.Event()
    done = []

    def worker():
        call.prepare_media()
        done.append(True)

    t1 = threading.Thread(target=worker, daemon=True)
    t2 = threading.Thread(target=worker, daemon=True)
    t1.start()
    assert fake_socket_mod.bind_entered.wait(2.0)
    t2.start()
    fake_socket_mod.bind_release.set()
    t1.join(2.0)
    t2.join(2.0)
    assert not t1.is_alive() and not t2.is_alive()
    assert len(done) == 2
    assert call.rtp is not None
    # exactly one candidate published; every other created socket is closed
    published = call.rtp
    others = [s for s in fake_socket_mod.instances if s is not published]
    assert all(s.closed for s in others), "loser candidate leaked"


class FakeSock:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append(data if isinstance(data, bytes) else data.encode())

    def close(self):
        pass


def test_answer_path_after_cancel_emits_only_480(fake_socket_mod):
    ata = make_ata()
    ata.sip_sock = FakeSock()
    ata.sip_stop = threading.Event()
    ata.reg_event = threading.Event()
    ata.sip_local_port = 5062
    ata.usb_active = True
    ata.usb_owner = threading.get_ident()
    import unittest.mock as mock
    ata.dev = mock.Mock()
    call = SIPCall(ata, 'incoming')
    call.incoming = True
    call.from_tag = 'ftag'
    call.invite_text = INVITE
    call.remote = ('127.0.0.1', 5060)
    call.sock = ata.sip_sock
    call.last_request = INVITE
    ata.call = call
    ata.ringing = True
    ata.shutdown_requested.set()   # cancel before OFFHOOK answer path
    ata.answer_incoming()          # must be a no-op now
    ata._detach_call()             # finalizer sends the correlated 480
    methods = [m.split(b' ')[1].decode() for m in ata.sip_sock.sent
               if m.startswith(b'SIP/2.0')]
    assert methods == ['480'], methods
    assert not any(m.startswith(b'SIP/2.0 200') or m.startswith(b'SIP/2.0 500')
                   for m in ata.sip_sock.sent)
