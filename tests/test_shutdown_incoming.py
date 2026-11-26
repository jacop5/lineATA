"""TASK-0026 incoming-call shutdown/admission audit (mock-only, no real
sockets/USB/network). Audits HEAD 127ff1e BEFORE implementation fixes.

H1: an already-ringing unanswered incoming INVITE must receive a correlated
final SIP response (480/486 — matching Call-ID/CSeq/Via, To-tag like the 180)
before the socket closes on shutdown — and never a BYE (unanswered dialog).
H2: admission race — deterministic barriers show whether a stale flag read
inside start_ringing can mutate state after shutdown's snapshot.
"""
import re
import threading
from unittest.mock import Mock

import pytest

from bridge.ata import LinearATA, HANGUP, RING_OFF, CALL_ANNOUNCE, sdp_local, sip_header

CFG = {'login': 'test', 'password': 'test-only', 'domain': 'example.invalid',
       'pbx_ip': '127.0.0.1', 'pbx_port': 5060, 'local_ip': '127.0.0.1',
       'sip_port': 5062}
ADDR = ('127.0.0.1', 5060)


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
    return a


def invite(cid='incoming-1', cseq=7):
    body = sdp_local('127.0.0.1', 24000)
    return ('INVITE sip:test@example.invalid SIP/2.0\r\n'
            'Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKin1\r\n'
            'From: <sip:caller@example.invalid>;tag=caller1\r\n'
            'To: <sip:test@example.invalid>\r\n'
            'Contact: <sip:caller@127.0.0.1:5060>\r\n'
            f'Call-ID: {cid}\r\nCSeq: {cseq} INVITE\r\n'
            'Content-Type: application/sdp\r\n'
            f'Content-Length: {len(body)}\r\n\r\n{body}')


def wire(ata):
    """Chronological walk of the socket's mock_calls: (label, closes_so_far,
    raw_text) per sendto; close entries increment closes_so_far."""
    out = []
    closes = 0
    for mc in ata.fake_sock.mock_calls:
        if mc[0] == 'close':
            closes += 1
        elif mc[0] == 'sendto':
            data = mc[1][0]
            text = data.decode() if isinstance(data, bytes) else data
            label = text.split(' ', 2)[1] if text.startswith('SIP/2.0') else text.split(' ', 1)[0]
            out.append((label, closes, text))
    return out


def written_frames(ata):
    return [c.args[0] for c in ata.write.call_args_list]


# ---------------------------------------------------------------- H1
def test_ringing_incoming_invite_gets_final_response_before_socket_close(ata):
    req = invite()
    ata.handle_sip_message(req, ADDR)                 # admitted: ringing
    assert ata.ringing is True and ata.call is not None
    pre = [lbl for lbl, _, _ in wire(ata)]
    assert pre == ['100', '180']
    ata.usb_active = True                # so the teardown RING_OFF is queued
    ata.request_shutdown()
    ata.shutdown(timeout=0.5)
    w = wire(ata)
    labels = [lbl for lbl, _, _ in w]
    close_count = ata.fake_sock.close.call_count
    assert close_count == 1
    assert 'BYE' not in labels                        # never BYE an unanswered dialog
    # H1: a final response correlated to THIS INVITE, sent before close:
    finals = [(lbl, closes, text) for lbl, closes, text in w
              if lbl in ('480', '486') and closes < close_count]
    assert finals, 'no final response to ringing INVITE before close; wire=%r' % labels
    lbl, _, text = finals[0]
    assert sip_header(text, 'Call-ID') == sip_header(req, 'Call-ID')
    assert sip_header(text, 'CSeq') == sip_header(req, 'CSeq')
    assert sip_header(text, 'Via') == sip_header(req, 'Via')
    to180 = next(t for l, _, t in w if l == '180')
    assert re.search(r'tag=', sip_header(text, 'To'))
    assert sip_header(text, 'To') == sip_header(to180, 'To')   # same To-tag as the 180
    # teardown state: call detached, RING_OFF queued, no HANGUP frames written
    assert ata.call is None
    frames = written_frames(ata)
    assert any(fr in frames for fr in RING_OFF)
    assert not any(fr in frames for fr in HANGUP)


# ------------------------------------------------- preserved behavior pins
def test_established_incoming_call_gets_single_bye_on_shutdown(ata):
    ata.handle_sip_message(invite(), ADDR)
    call = ata.call
    # patch RTP/media BEFORE the OFFHOOK answer, so no real socket is touched;
    # the fake start_media mirrors the real one's state effect (established/media)
    call.prepare_media = Mock()
    call.rtp = Mock()

    def fake_start_media():
        call.established = True
        call.media = True

    call.start_media = fake_start_media
    ata.handle_frame(bytes([0xA5, 0x04, 0x01, 0x00]))  # OFFHOOK -> answer
    assert call.established is True
    ata.request_shutdown()
    ata.shutdown(timeout=0.5)
    labels = [lbl for lbl, _, _ in wire(ata)]
    assert labels.count('BYE') == 1
    assert labels.count('200') == 1                    # the answer's 200 only
    assert ata.call is None


def test_duplicate_invite_retransmits_cached_response(ata):
    ata.handle_sip_message(invite(), ADDR)
    ata.handle_sip_message(invite(), ADDR)             # retransmission
    ring180 = [t for l, _, t in wire(ata) if l == '180']
    assert len(ring180) == 2 and ring180[0] == ring180[1]


# ---------------------------------------------------------------- H2 (post-fix)
def test_admission_during_shutdown_snapshot_is_refused_not_orphaned(ata, monkeypatch):
    """Barrier: pause admission at the 100-Trying send (after on_invite's entry
    check), run request_shutdown + shutdown paused in the drain AFTER its
    snapshot, then release admission. With atomic start_ringing the admission
    must be refused under the state lock: no new call/ringing, no 180, no
    CALL_ANNOUNCE — and never orphaned."""
    paused = threading.Event()
    release = threading.Event()
    orig_send_response = ata.send_response

    def hooked(req, addr, code, reason, body='', to_tag=None):
        if code == 100 and not paused.is_set():
            paused.set()
            release.wait(5)
        return orig_send_response(req, addr, code, reason, body, to_tag)

    monkeypatch.setattr(ata, 'send_response', hooked)
    in_drain = threading.Event()
    release_shutdown = threading.Event()
    orig_drain = ata._drain_usb_queue

    def hooked_drain(deadline, max_drain=1.0):
        in_drain.set()
        release_shutdown.wait(5)   # shutdown paused after snapshot, before stop.set()
        return orig_drain(deadline, max_drain)

    monkeypatch.setattr(ata, '_drain_usb_queue', hooked_drain)
    admission = threading.Thread(target=lambda: ata.handle_sip_message(invite(), ADDR),
                                 daemon=True)
    admission.start()
    shutdown_th = None
    try:
        assert paused.wait(2)
        ata.request_shutdown()
        shutdown_th = threading.Thread(target=lambda: ata.shutdown(timeout=2.0), daemon=True)
        shutdown_th.start()
        assert in_drain.wait(2), 'shutdown not paused in drain after snapshot'
        release.set()                       # resume admission mid-shutdown
        admission.join(2)
        assert not admission.is_alive()
        # admission was refused atomically: no state mutation, no 180
        assert ata.call is None and ata.ringing is False
        assert not any(fr in written_frames(ata) for fr in CALL_ANNOUNCE)
        labels = [lbl for lbl, _, _ in wire(ata)]
        assert '180' not in labels
        assert '480' in labels              # refused INVITE got a final 480
        release_shutdown.set()
        shutdown_th.join(3)
        assert not shutdown_th.is_alive()
        assert ata.call is None             # nothing orphaned after finalizer
        # no SIP traffic after the socket close:
        w = wire(ata)
        closes = ata.fake_sock.close.call_count
        assert closes == 1
        assert all(c < closes for _, c, _ in w)
    finally:
        release.set()
        release_shutdown.set()
        admission.join(2)
        if shutdown_th is not None:
            shutdown_th.join(3)


def test_queued_announce_guards_evaluated_by_real_pump(ata, monkeypatch):
    """Admit a call pre-request with the real write path (frames queued in the
    ControlQueue), then request shutdown and pump as the fake USB owner: the
    queued CALL_ANNOUNCE guards must discard the frames (no announce reaches
    the device after the request). Guards are evaluated by the real
    ControlQueue.pump, not by inspecting Mock write args."""
    ata.usb_active = True
    ata.write = LinearATA.write.__get__(ata)      # real write -> ControlQueue
    ata.handle_sip_message(invite(), ADDR)
    assert ata.ringing and ata.call is not None
    queued = [c.data for c in list(ata.usb_commands.commands.queue)]
    assert any(bytes.fromhex(fr) in queued for fr in CALL_ANNOUNCE)
    ata.request_shutdown()
    # fake USB owner pumps the queue: guards run inside ControlQueue.pump
    ata.usb_owner = threading.get_ident()
    ata.dev = Mock()
    ata.drain_commands()
    written = [c.args[1] for c in ata.dev.write.call_args_list]
    assert not any(bytes.fromhex(fr) in written for fr in CALL_ANNOUNCE)
    # teardown RING_OFF frames are not announce frames and may pass:
    assert ata.dev.write.call_count <= len(RING_OFF)


def test_fatal_usb_stop_still_sends_final_480_while_listener_alive(ata):
    """Global USB stop with sip_stop still unset must not suppress the final
    SIP response: the fatal finalizer sends the correlated 480, never a BYE."""
    req = invite()
    ata.handle_sip_message(req, ADDR)
    assert ata.ringing
    ata.stop.set()                       # fatal USB stop
    assert not ata.sip_stop.is_set()
    ata._usb_fault_cleanup()             # USB finalizer path (also used by shutdown)
    w = wire(ata)
    labels = [lbl for lbl, _, _ in w]
    assert 'BYE' not in labels
    finals = [t for lbl, _, t in w if lbl == '480']
    assert finals, 'no final 480 on fatal path; wire=%r' % labels
    assert sip_header(finals[0], 'Call-ID') == sip_header(req, 'Call-ID')
    assert sip_header(finals[0], 'CSeq') == sip_header(req, 'CSeq')
    assert sip_header(finals[0], 'Via') == sip_header(req, 'Via')
    assert ata.call is None
