"""TASK-0042 targeted falsification of the TASK-0041 final-response fix.

Source is read-only here; only wire/transport barriers (fake SIP socket sendto,
queued USB write) are used — no private lock names in any barrier.

Concerns under test (from the TASK-0042 assignment):
1. answer-first ordering: answer_incoming sets established via start_media()
   BEFORE send_response(200) while holding the final lock; _detach_call reads
   `established` OUTSIDE that lock. Park the fake sendto on the 200 BEFORE it is
   recorded and run the real _detach_call concurrently: does a BYE escape
   before the 200? A sequential control cannot test this.
2. lock-scope claim: TASK-0041's report calls the final lock a leaf never held
   over USB writes, but the CANCEL branch holds it across stop_ringing() USB
   writes and close(). Park CANCEL inside the RING_OFF USB write (lock held)
   with an already in-flight answer parked after a queued ANSWER write, and
   probe with a concurrent _detach_call.
3. the 500 (prepare_media OSError) path revalidates and emits WITHOUT the final
   lock: park its sendto(500), let CANCEL finalize 487 under the lock, release,
   and see whether a 500 follows the 487.

Desired assertions are preserved as written; a failing desired assertion is a
demonstrated defect, not a green characterization.
"""
import socket as real_socket
import threading
import types
from unittest.mock import Mock

import pytest

import bridge.ata as ata_mod
from bridge.ata import ANSWER, CALL_ANNOUNCE, RING_OFF, LinearATA, sip_header

CFG = {'login': 't', 'password': 'x', 'domain': 'd', 'pbx_ip': '127.0.0.1',
       'pbx_port': 5060, 'local_ip': '127.0.0.1', 'sip_port': 5062, 'cold': True}
ADDR = ('127.0.0.1', 5060)

INVITE = ('INVITE sip:t@d SIP/2.0\r\n'
          'Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKinv1;rport\r\n'
          'From: <sip:69@d>;tag=caller1\r\n'
          'To: <sip:t@d>\r\n'
          'Call-ID: cid\r\nCSeq: 1 INVITE\r\n'
          'Contact: <sip:69@127.0.0.1:5060>\r\n'
          'Content-Type: application/sdp\r\nContent-Length: 54\r\n\r\n'
          'v=0\r\nc=IN IP4 127.0.0.1\r\ns=x\r\nm=audio 4000 RTP/AVP 0\r\n')

CANCEL = ('CANCEL sip:t@d SIP/2.0\r\n'
          'Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKinv1;rport\r\n'
          'From: <sip:69@d>;tag=caller1\r\nTo: <sip:t@d>\r\n'
          'Call-ID: cid\r\nCSeq: 1 CANCEL\r\nContent-Length: 0\r\n\r\n')


class FakeUDPSocket:
    instances = []
    bind_fail = False          # armed only by the 500-path test

    def __init__(self, *a, **k):
        self.closed = False
        FakeUDPSocket.instances.append(self)

    def bind(self, addr):
        if FakeUDPSocket.bind_fail:
            FakeUDPSocket.bind_fail = False
            raise real_socket.error('armed bind failure')

    def settimeout(self, t):
        pass

    def getsockname(self):
        return ('127.0.0.1', 43000 + len(FakeUDPSocket.instances))

    def recvfrom(self, n):
        raise real_socket.timeout()

    def close(self):
        self.closed = True


class FakeSIPSock:
    """Chronological wire log; sendto can be wrapped with park barriers."""

    def __init__(self):
        self.sent = []
        self.closed = False

    def sendto(self, data, addr):
        if self.closed:
            raise OSError('closed')
        self.sent.append(data if isinstance(data, bytes) else data.encode())

    def close(self):
        self.closed = True


class FakeDev:
    def __init__(self):
        self.writes = []

    def write(self, ep, data, timeout):
        self.writes.append(bytes(data))
        return len(data)


@pytest.fixture
def env(monkeypatch):
    FakeUDPSocket.instances = []
    FakeUDPSocket.bind_fail = False
    monkeypatch.setattr(ata_mod, 'socket', types.SimpleNamespace(
        AF_INET=real_socket.AF_INET, SOCK_DGRAM=real_socket.SOCK_DGRAM,
        timeout=real_socket.timeout, socket=FakeUDPSocket))
    ata = LinearATA(dict(CFG))
    ata.log = lambda *a: None
    ata.sip_sock = FakeSIPSock()
    ata.sip_stop = threading.Event()
    ata.reg_event = threading.Event()
    ata.sip_local_port = 5062
    ata.usb_active = True
    ata.usb_owner = threading.get_ident()
    ata.dev = FakeDev()
    return ata


def make_incoming(ata):
    """Admit through the REAL on_invite path so IDs/tags match the wire."""
    ata.handle_sip_message(INVITE, ADDR)
    call = ata.call
    assert call is not None and call.incoming
    assert call.call_id == 'cid' and call.invite_cseq == '1'
    return call


def wire(ata):
    return ata.sip_sock.sent


def first_lines(ata):
    return [m.split(b'\r\n')[0].decode() for m in ata.sip_sock.sent]


def invite_final_codes(ata):
    """Final (>=200) codes of responses to the INVITE transaction (CSeq: 1
    INVITE) only; the 200 answering the CANCEL transaction is excluded."""
    return [m.split(b' ')[1].decode() for m in ata.sip_sock.sent
            if m.startswith(b'SIP/2.0') and b'CSeq: 1 INVITE' in m
            and int(m.split(b' ')[1]) >= 200]


def join_bounded(*threads, timeout=2.0):
    for t in threads:
        t.join(timeout)
    return [t.is_alive() for t in threads]


def run_worker(target, *args):
    """Run a real method in a daemon thread and record EXPLICIT successful
    completion: a parked-then-resumed worker that crashes must fail the test,
    not silently satisfy a no-late-response assertion."""
    done = {}

    def wrapper():
        try:
            target(*args)
            done['ok'] = True
        except BaseException as exc:
            done['exc'] = repr(exc)

    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    return t, done


def assert_worker_ok(done):
    assert done.get('ok') is True and 'exc' not in done, done


def test_sequential_answer_then_detach_control_200_precedes_bye(env):
    """Sequential control: answer completes first, then shutdown detach.
    Cannot test the race — included to show the ordering only holds here."""
    ata = env
    call = make_incoming(ata)
    ata.answer_incoming()
    assert '200' in [m.split(b' ')[1].decode() for m in wire(ata)
                     if m.startswith(b'SIP/2.0')]
    ata.shutdown_requested.set()
    ata.stop.set()
    ata._detach_call()
    w = wire(ata)
    i200 = next(i for i, m in enumerate(w) if m.startswith(b'SIP/2.0 200'))
    ibye = next(i for i, m in enumerate(w) if m.startswith(b'BYE '))
    assert i200 < ibye                       # holds sequentially
    assert len([m for m in w if m.startswith(b'BYE ')]) == 1
    assert invite_final_codes(ata) == ['200']
    assert not any(c in ('480', '487') for c in invite_final_codes(ata))
    call.close()


def test_concurrent_detach_waits_for_parked_200_before_bye(env):
    """Park the fake sendto on the 200 BEFORE recording it; answer is then
    inside the final lock with established already set. Run the real
    _detach_call concurrently and observe whether BYE precedes the 200."""
    ata = env
    call = make_incoming(ata)
    orig_sendto = ata.sip_sock.sendto
    parked200 = threading.Event()
    release200 = threading.Event()

    def sendto_spy(data, addr):
        if data.startswith(b'SIP/2.0 200'):
            parked200.set()
            assert release200.wait(2.0), 'barrier timeout'
        orig_sendto(data, addr)

    ata.sip_sock.sendto = sendto_spy
    t, done = run_worker(ata.answer_incoming)
    assert parked200.wait(2.0), 'answer never reached the 200 send'
    # answer is parked inside send_response(200): final lock held, established
    # already set by start_media, but the 200 is NOT yet on the wire:
    assert call.established and call.final_sent
    assert not any(m.startswith(b'SIP/2.0 200') for m in wire(ata))
    detached = threading.Event()

    def detach():
        try:
            ata._detach_call()
        finally:
            detached.set()

    ata.shutdown_requested.set()
    ata.stop.set()
    dt, ddone = run_worker(detach)
    try:
        # A correct detach waits on the pending final send, rather than
        # emitting BYE before it. The previous implementation completes here.
        assert not detached.wait(0.2), first_lines(ata)
        assert not any(m.startswith(b'BYE ') for m in wire(ata))
    finally:
        release200.set()
        alive = join_bounded(t, dt)
    assert not any(alive), alive
    assert_worker_ok(done)
    assert_worker_ok(ddone)
    if call.thread is not None:
        call.close()
        assert not join_bounded(call.thread)[0]
    w = wire(ata)
    i200 = next(i for i, m in enumerate(w) if m.startswith(b'SIP/2.0 200'))
    ibye = next(i for i, m in enumerate(w) if m.startswith(b'BYE '))
    r200 = w[i200].decode()
    assert 'Call-ID: cid' in r200 and 'CSeq: 1 INVITE' in r200
    r180 = next(m.decode() for m in w if m.startswith(b'SIP/2.0 180'))
    assert sip_header(r200, 'To') == sip_header(r180, 'To')
    # DESIRED (TASK-0041 acceptance): answer-first must finish the 200 before
    # any shutdown BYE. This is the falsification target.
    assert i200 < ibye, \
        f'BYE preceded the 200 on the wire: {first_lines(ata)}'


def test_cancel_vs_inflight_answer_wire_single_487(env):
    """CANCEL racing an ALREADY in-flight answer parked after a queued ANSWER
    write: wire correctness — the 487 wins, no late 200/480/500, no media."""
    ata = env
    call = make_incoming(ata)
    orig_write = ata.write
    entered = threading.Event()
    release = threading.Event()
    state = {'n': 0}

    def write_spy(hexstr, timeout=2000, guard=None):
        orig_write(hexstr, timeout, guard)   # real queue submission first
        if hexstr in ANSWER or hexstr == CALL_ANNOUNCE[0]:
            state['n'] += 1
            if state['n'] == 1:              # park after first queued ANSWER write
                entered.set()
                assert release.wait(2.0), 'barrier timeout'

    ata.write = write_spy
    t, done = run_worker(ata.answer_incoming)
    assert entered.wait(2.0), 'answer never reached the ANSWER write'
    try:
        ata.handle_sip_message(CANCEL, ADDR)   # real CANCEL path -> 487 + close
        assert invite_final_codes(ata) == ['487']
    finally:
        release.set()
    assert not join_bounded(t)[0]
    assert_worker_ok(done)                     # resumed answer completed cleanly
    finals = invite_final_codes(ata)
    assert finals == ['487'], f'finals after cancel-first race: {finals}'
    assert not call.established and not call.media and call.closed
    assert not any(m.startswith(b'SIP/2.0 200') and b'CSeq: 1 INVITE' in m
                   for m in wire(ata))
    call.close()


def test_final_lock_held_across_cancel_usb_writes(env):
    """Falsify the report claim that the final lock is a leaf never held over
    USB writes: park CANCEL inside stop_ringing's RING_OFF USB write (it holds
    the final lock there) while an in-flight answer waits, then probe with a
    concurrent _detach_call — a leaf lock would let detach finalize without
    waiting for the parked USB write."""
    ata = env
    call = make_incoming(ata)
    orig_write = ata.write
    answer_entered = threading.Event()
    answer_release = threading.Event()
    ring_parked = threading.Event()
    ring_release = threading.Event()
    state = {'an': 0, 'ring': False}

    def write_spy(hexstr, timeout=2000, guard=None):
        if hexstr == RING_OFF[0] and not state['ring']:
            state['ring'] = True
            orig_write(hexstr, timeout, guard)
            ring_parked.set()
            assert ring_release.wait(2.0), 'barrier timeout'
            return                            # parked INSIDE the CANCEL lock
        orig_write(hexstr, timeout, guard)
        if hexstr in ANSWER or hexstr == CALL_ANNOUNCE[0]:
            state['an'] += 1
            if state['an'] == 1:
                answer_entered.set()
                assert answer_release.wait(2.0), 'barrier timeout'

    ata.write = write_spy
    at, adone = run_worker(ata.answer_incoming)
    assert answer_entered.wait(2.0), 'in-flight answer never parked'
    ct, cdone = run_worker(ata.handle_sip_message, CANCEL, ADDR)
    assert ring_parked.wait(2.0), 'CANCEL never parked in the RING_OFF write'
    assert invite_final_codes(ata) == ['487']   # 487 sent under the held lock
    answer_release.set()                         # in-flight answer proceeds
    dt, ddone = run_worker(ata._detach_call)
    try:
        # USB cleanup must not retain the final-response lock. Bounded join
        # observes completion directly instead of sleeping then guessing.
        dt.join(0.3)
        assert not dt.is_alive(), 'detach blocked on CANCEL USB cleanup' 
    finally:
        ring_release.set()
    alive = join_bounded(ct, at, dt)
    assert not any(alive), f'threads did not finish: {alive}'
    assert_worker_ok(adone)
    assert_worker_ok(cdone)
    assert_worker_ok(ddone)
    finals = invite_final_codes(ata)
    print('observed: detach blocked by parked USB write under final lock;',
          'finals:', finals, 'wire:', first_lines(ata))
    assert finals == ['487'], finals             # wire decision itself is correct
    call.close()


def test_500_choice_serialized_against_cancel(env):
    """A 500 chosen before CANCEL must complete as the sole INVITE final.
    Park its send, run CANCEL concurrently, then release both participants.
    The previous implementation emitted contradictory 487 and 500 finals."""
    ata = env
    call = make_incoming(ata)
    FakeUDPSocket.bind_fail = True               # prepare_media raises OSError
    orig_sendto = ata.sip_sock.sendto
    parked500 = threading.Event()
    release500 = threading.Event()

    def sendto_spy(data, addr):
        if data.startswith(b'SIP/2.0 500'):
            parked500.set()
            assert release500.wait(2.0), 'barrier timeout'
        orig_sendto(data, addr)

    ata.sip_sock.sendto = sendto_spy
    t, done = run_worker(ata.answer_incoming)
    assert parked500.wait(2.0), 'answer never reached the 500 send'
    # The 500 path reached its final send first. CANCEL must wait for this
    # decision, then emit no contradictory 487. Running CANCEL synchronously
    # would deadlock a correct implementation against our parked send.
    ct, cdone = run_worker(ata.handle_sip_message, CANCEL, ADDR)
    try:
        ct.join(0.2)
        assert ct.is_alive(), first_lines(ata)
        assert invite_final_codes(ata) == []
    finally:
        release500.set()
        alive = join_bounded(t, ct)
    assert not any(alive), alive
    assert_worker_ok(done)
    assert_worker_ok(cdone)
    finals = invite_final_codes(ata)
    assert finals == ['500'], f'contradictory INVITE finals: {finals}'
    assert call.closed and not call.media
    call.close()
