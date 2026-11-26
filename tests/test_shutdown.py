"""Offline graceful-shutdown tests (TASK-0018): mocked socket/threads, no
real devices, sockets, network or sleeps. Baseline run before implementation
fails with AttributeError (API absent) — that is the missing-feature evidence.
"""
import signal
import socket
import threading
import time
from unittest.mock import Mock

import pytest

from bridge.ata import LinearATA, HANGUP, RING_OFF, TONE_OFF, sdp_local

CFG = {'login': 'test', 'password': 'test-only', 'domain': 'example.invalid',
       'pbx_ip': '127.0.0.1', 'pbx_port': 5060, 'local_ip': '127.0.0.1',
       'sip_port': 5062}
ADDR = ('127.0.0.1', 5060)
OUR_CONTACT = 'sip:test@127.0.0.1:5062'
CHALLENGE = 'Digest realm="example.invalid", nonce="d1", qop="auth"'


class FakeThread:
    def __init__(self, *a, **k):
        self.target = k.get('target')
        self.daemon = k.get('daemon')
        self.joined = []

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


@pytest.fixture
def ata(monkeypatch):
    """start_sip(register=False) on a mocked socket; threads recorded, not run."""
    sock = Mock()
    sock.getsockname.return_value = ('127.0.0.1', 5062)
    monkeypatch.setattr('bridge.ata.socket.socket', Mock(return_value=sock))
    monkeypatch.setattr('bridge.ata.threading', ThreadingShim())
    a = LinearATA(dict(CFG))
    a.write = Mock()
    a.start_sip(register=False)
    a.fake_sock = sock
    return a


@pytest.fixture
def ata_real(monkeypatch):
    """Real threading (for join-order tests), mocked socket, listener parked."""
    sock = Mock()
    sock.getsockname.return_value = ('127.0.0.1', 5062)
    sock.recvfrom.side_effect = socket.timeout
    monkeypatch.setattr('bridge.ata.socket.socket', Mock(return_value=sock))
    a = LinearATA(dict(CFG))
    a.write = Mock()
    a.start_sip(register=False)
    a.fake_sock = sock
    yield a
    a.stop.set()  # works at baseline and after implementation
    ss = getattr(a, 'sip_stop', None)
    if ss is not None:
        ss.set()
    th = getattr(a, 'sip_listener_thread', None)
    if th is not None:
        th.join(timeout=2)


def respond(request, status, extra=()):
    req = request.decode() if isinstance(request, bytes) else request
    head, sep, body = req.partition('\r\n\r\n')
    out = [status]
    for line in head.split('\r\n')[1:]:
        if line.lower().startswith('contact:'):
            continue
        out.append(line)
    out.extend(extra)
    return '\r\n'.join(out) + sep + body


def invite(cid='sh-invite', cseq=42):
    body = sdp_local('127.0.0.1', 24000)
    return ('INVITE sip:test@example.invalid SIP/2.0\r\n'
            'Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKsh\r\n'
            'From: <sip:caller@example.invalid>;tag=caller\r\n'
            'To: <sip:test@example.invalid>\r\n'
            'Contact: <sip:caller@127.0.0.1:5060>\r\n'
            f'Call-ID: {cid}\r\nCSeq: {cseq} INVITE\r\n'
            f'Content-Type: application/sdp\r\nContent-Length: {len(body)}\r\n\r\n{body}')


def registers(sock):
    return [c.args[0] for c in sock.sendto.call_args_list
            if c.args[0].startswith(b'REGISTER')]


# ---------------------------------------------------------------- handler API
def test_request_shutdown_sets_event_with_no_io(ata):
    assert ata.shutdown_requested.is_set() is False
    ata.request_shutdown()
    assert ata.shutdown_requested.is_set() is True
    ata.request_shutdown(signal.SIGTERM, None)
    ata.fake_sock.sendto.assert_not_called()
    ata.fake_sock.close.assert_not_called()
    ata.write.assert_not_called()
    assert ata.stop.is_set() is False


def test_shutdown_fresh_instance_safe_and_idempotent():
    a = LinearATA(dict(CFG))  # never run/started
    r1 = a.shutdown(timeout=1.0)
    r2 = a.shutdown(timeout=1.0)
    assert isinstance(r1, str) and r2 == 'already-shutdown'


# ---------------------------------------------------------------- suppression
def test_invite_after_request_gets_480_and_never_rings(ata):
    ata.request_shutdown()
    ata.handle_sip_message(invite(), ADDR)
    payloads = [c.args[0] for c in ata.fake_sock.sendto.call_args_list]
    assert any(p.startswith(b'SIP/2.0 480') for p in payloads)
    assert ata.call is None and ata.ringing is False
    ata.write.assert_not_called()


def test_hook_and_dtmf_after_request_are_suppressed(ata):
    ata.request_shutdown()
    ata.handle_frame(bytes([0xA5, 0x04, 0x01, 0x00]))   # OFFHOOK
    ata.handle_frame(bytes([0xA4, 0x04, ord('5'), 0x00]))  # DTMF '5'
    assert ata.call is None and ata.dialing == ''
    ata.write.assert_not_called()


def test_try_dial_after_request_does_nothing(ata):
    ata.request_shutdown()
    ata.dialing = '69'
    ata.try_dial()
    assert ata.dialing == '' and ata.call is None
    ata.write.assert_not_called()


# ---------------------------------------------------------------- teardown frames
def test_established_call_queues_captured_hangup_and_detaches_once(monkeypatch, ata):
    ata.usb_active = True
    ata.write = LinearATA.write.__get__(ata)  # real write -> queues into ControlQueue
    call = Mock()
    call.established = True
    call.media = True
    ata.call = call
    snapshots = []
    monkeypatch.setattr(ata, '_drain_usb_queue',
                        lambda deadline: snapshots.append(
                            [c.data for c in list(ata.usb_commands.commands.queue)]) or True)
    ata.request_shutdown()
    result = ata.shutdown(timeout=2.0)
    assert snapshots and all(bytes.fromhex(fr) in snapshots[0] for fr in HANGUP)
    assert ata.call is None
    call.hangup.assert_called_once_with()
    assert 'call-detached' in result


def test_ringing_queues_ring_off_not_hangup(monkeypatch, ata):
    ata.usb_active = True
    ata.write = LinearATA.write.__get__(ata)
    ata.ringing = True
    snapshots = []
    monkeypatch.setattr(ata, '_drain_usb_queue',
                        lambda deadline: snapshots.append(
                            [c.data for c in list(ata.usb_commands.commands.queue)]) or True)
    ata.request_shutdown()
    ata.shutdown(timeout=2.0)
    assert snapshots and all(bytes.fromhex(fr) in snapshots[0] for fr in RING_OFF)
    assert not any(bytes.fromhex(fr) in snapshots[0] for fr in HANGUP)


def test_idle_shutdown_queues_nothing(ata):
    ata.request_shutdown()
    ata.shutdown(timeout=2.0)
    ata.write.assert_not_called()  # no idle HANGUP/TONE_OFF


# ---------------------------------------------------------------- deregistration
def test_deregister_challenge_then_success_within_deadline(ata):
    def sendto(data, addr):
        if data.startswith(b'REGISTER') and b'Authorization' not in data:
            ata.handle_sip_message(respond(
                data, 'SIP/2.0 401 Unauthorized',
                extra=[f'WWW-Authenticate: {CHALLENGE}']), addr)
        elif data.startswith(b'REGISTER'):
            ata.handle_sip_message(respond(data, 'SIP/2.0 200 OK'), addr)
    ata.fake_sock.sendto.side_effect = sendto
    ata.request_shutdown()
    result = ata.shutdown(timeout=3.0)
    regs = registers(ata.fake_sock)
    assert any(b'expires=0' in r and OUR_CONTACT.encode() in r for r in regs)
    assert any(b'expires=0' in r and b'Authorization: Digest' in r for r in regs)
    assert all(b'Contact: *' not in r for r in regs)
    assert 'deregister:ok' in result


def test_deregister_works_after_global_usb_stop(ata):
    ata.stop.set()  # fatal USB stop must not preclude Contact removal
    ata.request_shutdown()
    ata.shutdown(timeout=2.0)
    regs = registers(ata.fake_sock)
    assert regs and any(b'expires=0' in r for r in regs)


def test_deregister_no_response_is_bounded_and_releases(ata):
    ata.request_shutdown()
    t0 = time.monotonic()
    result = ata.shutdown(timeout=0.5)
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0
    assert 'deregister:timeout' in result  # never claims a lost response succeeded
    ata.fake_sock.close.assert_called_once()


def test_registrar_joins_before_deregister_is_sent(ata_real):
    order = []
    done = threading.Event()

    def registrar_body():
        while not ata_real.stop.is_set():
            ata_real.stop.wait(0.02)
        order.append('registrar-exit')
        done.set()

    th = threading.Thread(target=registrar_body)
    th.start()
    ata_real.registrar_thread = th

    def sendto(data, addr):
        if data.startswith(b'REGISTER'):
            order.append('register-sent')
    ata_real.fake_sock.sendto.side_effect = sendto
    ata_real.request_shutdown()
    ata_real.shutdown(timeout=3.0)
    th.join(timeout=2)
    assert 'registrar-exit' in order and 'register-sent' in order
    assert order.index('registrar-exit') < order.index('register-sent')


# ---------------------------------------------------------------- ordering / idempotence
def test_socket_closes_last_after_final_traffic(ata):
    manager = Mock()
    manager.attach_mock(ata.fake_sock.sendto, 'sendto')
    manager.attach_mock(ata.fake_sock.close, 'close')
    ata.request_shutdown()
    ata.shutdown(timeout=2.0)
    from unittest.mock import call as mcall
    calls = manager.mock_calls
    assert mcall.close() in calls and calls[-1] == mcall.close()


def test_second_shutdown_sends_no_duplicate_bye_or_register(monkeypatch, ata):
    monkeypatch.setattr(ata, '_drain_usb_queue', lambda deadline: True)
    call = Mock()
    call.established = True
    call.media = True
    ata.call = call
    ata.request_shutdown()
    ata.shutdown(timeout=2.0)
    sends = ata.fake_sock.sendto.call_count
    closes = ata.fake_sock.close.call_count
    ata.shutdown(timeout=2.0)
    assert call.hangup.call_count == 1          # no duplicate BYE
    assert ata.fake_sock.sendto.call_count == sends  # no duplicate REGISTER
    assert ata.fake_sock.close.call_count == closes == 1


def test_fatal_usb_path_still_releases_and_deregisters(monkeypatch, ata):
    dispose = Mock()
    monkeypatch.setattr('bridge.ata.usb.util.dispose_resources', dispose)
    call = Mock()
    call.established = True
    call.media = True
    ata.call = call
    ata.usb_active = True
    ata.dev = Mock()
    ata.stop.set()             # usb_loop's except branch sets stop before cleanup
    ata._usb_fault_cleanup()   # queue cleared, call detached
    ata._usb_release()
    assert ata.stop.is_set() and ata.dev is None and ata.call is None
    hangups = call.hangup.call_count
    ata.request_shutdown()
    ata.shutdown(timeout=2.0)
    assert call.hangup.call_count == hangups      # no second hangup
    assert dispose.call_count == 1                # no duplicate dispose
    assert any(b'expires=0' in r for r in registers(ata.fake_sock))  # still deregisters
    ata.fake_sock.close.assert_called_once()


def test_shutdown_error_still_releases_resources(ata, monkeypatch):
    def boom(deadline):
        raise RuntimeError('drain exploded')
    monkeypatch.setattr(ata, '_drain_usb_queue', boom)
    ata.request_shutdown()
    result = ata.shutdown(timeout=2.0)
    assert isinstance(result, str) and result
    ata.fake_sock.close.assert_called_once()     # socket still closed
    assert ata.sip_stop.is_set()                  # listener stopped
    assert ata.stop.is_set()                      # global stop signaled


# ---------------------------------------------------------------- hangup race
def _wire_methods(sock):
    return [c.args[0].split(b' ', 1)[0].decode()
            for c in sock.sendto.call_args_list]


def test_hangup_race_no_duplicate_cancel_or_bye_but_late200_still_gets_bye(ata):
    """CANCEL on a pending INVITE, then a racing 200 establishes the dialog:
    the required BYE must still be sent exactly once; repeat hangup() calls
    must not duplicate CANCEL or BYE (per-method sent state, no I/O under lock)."""
    from bridge.ata import SIPCall
    call = SIPCall(ata, '69')
    call.sock = ata.fake_sock
    call.remote = ADDR
    call.last_request = ('INVITE sip:69@example.invalid SIP/2.0\r\n'
                         'Via: SIP/2.0/UDP 127.0.0.1:5062;branch=z9hG4bKabc;rport\r\n'
                         'From: <sip:test@example.invalid>;tag=t\r\n'
                         'To: <sip:69@example.invalid>\r\n'
                         f'Call-ID: {call.call_id}\r\nCSeq: 1 INVITE\r\n'
                         'Max-Forwards: 70\r\nContent-Length: 0\r\n\r\n')
    call.hangup()  # pending INVITE -> CANCEL
    call.hangup()  # duplicate -> suppressed
    assert _wire_methods(ata.fake_sock) == ['CANCEL']
    # late 200 arrives: worker path hangs up the now-established dialog
    call.established = True
    call.target_uri = 'sip:69@127.0.0.1:5060'
    call.local_from = '<sip:test@example.invalid>;tag=t'
    call.remote_to = '<sip:69@example.invalid>;tag=r'
    call.hangup()  # CANCEL->late-200 requires ACK+BYE: BYE sent once
    call.hangup()  # shutdown-side duplicate -> suppressed
    assert _wire_methods(ata.fake_sock) == ['CANCEL', 'BYE']


# ---------------------------------------------------------------- main wiring
def test_main_installs_signal_handlers_and_calls_shutdown(monkeypatch):
    import bridge.ata as ata_mod
    if not hasattr(LinearATA, 'request_shutdown'):
        pytest.skip('shutdown API not implemented yet (baseline)')
    prepared = LinearATA(dict(CFG))
    prepared.run = Mock(return_value=True)
    prepared.start_sip = Mock()
    prepared.shutdown = Mock(return_value='ok')
    monkeypatch.setattr(ata_mod, 'LinearATA', lambda cfg: prepared)
    monkeypatch.setattr('sys.argv', ['ata', '--pbx', '127.0.0.1', '--login', 't',
                                     '--password', 'x', '--local-ip', '127.0.0.1'])
    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)

    def trigger():
        time.sleep(0.2)
        prepared.request_shutdown(signal.SIGTERM, None)
    threading.Thread(target=trigger).start()
    ata_mod.main()
    prepared.shutdown.assert_called_once()
    assert signal.getsignal(signal.SIGTERM) == old_term  # handlers restored
    assert signal.getsignal(signal.SIGINT) == old_int
