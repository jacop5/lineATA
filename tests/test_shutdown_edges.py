"""TASK-0023 shutdown edge regressions (mock-only; no real sockets/USB/network).

Promotes the TASK-0022 / lead-lifecycle-counterexamples findings into
meaningful public-behavior regressions. Expected to FAIL against the frozen
pre-fix source (hash recorded in report) and PASS once the lead's production
fixes land. Uses only existing public API: request_shutdown/shutdown,
shutdown_requested/sip_stop, thread-handle fields, SIPCall, and the mock
socket as fake UDP transport.
"""
import threading
import time as time_mod
from unittest.mock import Mock

import pytest

from bridge.ata import LinearATA, SIPCall, sdp_local

CFG = {'login': 'test', 'password': 'test-only', 'domain': 'example.invalid',
       'pbx_ip': '127.0.0.1', 'pbx_port': 5060, 'local_ip': '127.0.0.1',
       'sip_port': 5062}
ADDR = ('127.0.0.1', 5060)


class _Delegate:
    def __init__(self, target, **overrides):
        object.__setattr__(self, '_target', target)
        object.__setattr__(self, '_overrides', overrides)

    def __getattr__(self, name):
        overrides = object.__getattribute__(self, '_overrides')
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, '_target'), name)


class FakeEvent:
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


class FakeClock:
    def __init__(self, step=0.05):
        self.t = 1000.0
        self.step = step

    def monotonic(self):
        self.t += self.step
        return self.t

    def time(self):
        return self.t


@pytest.fixture
def ata(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, *a, **k):
            self.target = k.get('target')
            started.append(self)

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


def sent_methods(ata):
    out = []
    for c in ata.fake_sock.sendto.call_args_list:
        data = c.args[0]
        text = data.decode() if isinstance(data, bytes) else data
        out.append(text.split(' ', 1)[0])
    return out


def registers(ata):
    return [c.args[0] for c in ata.fake_sock.sendto.call_args_list
            if c.args[0].startswith(b'REGISTER')]


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


def final_200(request):
    req = request.decode() if isinstance(request, bytes) else request
    head, sep, _ = req.partition('\r\n\r\n')
    body = sdp_local('127.0.0.1', 24000)
    out = ['SIP/2.0 200 OK']
    for line in head.split('\r\n')[1:]:
        if line.lower().startswith(('to:', 'content-length:', 'content-type:')):
            if line.lower().startswith('to:'):
                line += ';tag=remote1'
            else:
                continue
        out.append(line)
    out += ['Contact: <sip:callee@127.0.0.1:5060>',
            'Content-Type: application/sdp', f'Content-Length: {len(body)}']
    return '\r\n'.join(out) + '\r\n\r\n' + body


def pending_outgoing_call(ata):
    call = SIPCall(ata, '123')
    call.sock = ata.fake_sock          # fake UDP transport
    call.remote = ADDR
    call.last_request = invite_request(call)
    call.invite_cseq = call.cseq
    call.rtp = Mock()                  # MockRTP stand-in, no real socket
    call.prepare_media = Mock()
    call.start_media = Mock()
    ata.call = call
    ata.invite_transactions[call.call_id] = call
    return call


# (1) created-but-never-started threads must not break shutdown/release
def test_shutdown_with_unstarted_registrar_and_usb_threads_releases(ata, monkeypatch):
    dispose = Mock()
    monkeypatch.setattr('bridge.ata.usb.util.dispose_resources', dispose)
    ata.registrar_thread = threading.Thread(target=lambda: None)  # never started
    ata.usb_thread = threading.Thread(target=lambda: None)        # never started
    ata.dev = Mock()                                               # inactive device
    ata.request_shutdown()
    res = ata.shutdown(timeout=1.0)          # must not raise RuntimeError
    assert isinstance(res, str)
    assert ata.fake_sock.close.called         # socket released
    assert ata.sip_stop.is_set()
    assert ata.dev is None and dispose.called  # inactive device released


# (2)+(6) expired deadline: zero packets, no false success string
def test_expired_shutdown_deadline_sends_zero_packets(ata, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr('bridge.ata.time', _Delegate(
        time_mod, monotonic=clock.monotonic, time=clock.time))
    ata.reg_event = FakeEvent()  # never set: no real response waits
    ata.request_shutdown()
    res = ata.shutdown(timeout=0.0)  # deadline already expired
    assert registers(ata) == []                     # zero packets after expiry
    assert 'deregister:ok' not in str(res)          # truthy string != success
    assert ata.fake_sock.close.called               # resources still released


# (3) pending INVITE explicitly closed by shutdown return, even if the USB
# finalizer already detached it
def test_pending_invite_closed_by_shutdown_return_after_usb_finalizer(ata):
    call = pending_outgoing_call(ata)
    ata._usb_fault_cleanup()             # USB finalizer detaches the call first
    assert ata.call is None
    ata.request_shutdown()
    ata.shutdown(timeout=1.0)            # bounded grace within deadline
    assert call.closed is True           # explicitly closed by shutdown return
    call.rtp.close.assert_called()       # resources released


def respond_simple(request, status):
    req = request.decode() if isinstance(request, bytes) else request
    head, sep, body = req.partition('\r\n\r\n')
    out = [status]
    for line in head.split('\r\n')[1:]:
        if line.lower().startswith(('contact:', 'content-length:')):
            continue
        out.append(line)
    out.append('Content-Length: 0')
    return '\r\n'.join(out) + sep + body


# (4) CANCEL + racing 200 inside the listener-alive window => ACK + single BYE
def test_cancel_racing_200_during_dereg_window_exactly_ack_single_bye(ata):
    """A real bounded worker consumes call.responses BEFORE shutdown; the
    racing 200 is injected during the deregistration REGISTER send (i.e. after
    CANCEL, while the socket is still open); BYE must be observed while the
    socket is open. The REGISTER itself gets a matching 200 so the window is
    genuinely successful."""
    call = pending_outgoing_call(ata)
    bye_seen = threading.Event()
    state_at_bye = {}
    fed = []

    def sendto(data, addr):
        text = data.decode() if isinstance(data, bytes) else data
        if text.startswith('BYE'):
            bye_seen.set()
            state_at_bye['socket_open'] = (ata.fake_sock.close.call_count == 0
                                            and not ata.sip_stop.is_set())
        if text.startswith('REGISTER') and not fed:
            fed.append(True)
            # CANCEL was already sent (shutdown detaches before deregistering),
            # socket still open, listener still alive:
            assert not ata.sip_stop.is_set()
            assert ata.fake_sock.close.call_count == 0
            assert sent_methods(ata).count('CANCEL') == 1
            ata.handle_sip_message(final_200(call.last_request), addr)   # racing 200
            ata.handle_sip_message(respond_simple(data, 'SIP/2.0 200 OK'), addr)  # dereg 200

    ata.fake_sock.sendto.side_effect = sendto
    worker = threading.Thread(target=call.handle_responses, args=(5,), daemon=True)
    worker.start()  # real bounded worker, started before shutdown
    try:
        ata.request_shutdown()
        res = ata.shutdown(timeout=2.0)
        worker.join(2)
        assert not worker.is_alive(), 'worker did not finish bounded'
        assert fed, 'racing 200 / dereg 200 not injected during the window'
        assert bye_seen.is_set(), 'BYE not observed'
        assert state_at_bye.get('socket_open') is True, 'BYE sent after socket closed'
        methods = sent_methods(ata)
        assert methods.count('CANCEL') == 1
        assert methods.count('ACK') == 1
        assert methods.count('BYE') == 1     # single BYE, never duplicated
        assert 'deregister:ok' in str(res)   # deregistration genuinely succeeded
        assert call.closed is True
    finally:
        if worker.is_alive():
            worker.join(2)
        if not call.closed:
            call.close()


# (5) no new ring frames after request or fatal stop; hook answer suppressed
def test_ring_tick_after_request_queues_no_ring_frames(ata):
    ata.request_shutdown()
    ata.ringing = True
    ata.ring_deadline = 0.0              # due now
    ata.ring_tick(1.0)
    ata.write.assert_not_called()        # no RING_ON / RING_POLL queued


def test_ring_tick_after_fatal_stop_queues_no_ring_frames(ata):
    ata.stop.set()                       # fatal USB stop
    ata.ringing = True
    ata.ring_deadline = 0.0
    ata.ring_tick(1.0)
    ata.write.assert_not_called()


def test_offhook_while_ringing_after_request_does_not_answer(ata):
    ata.request_shutdown()
    ata.ringing = True
    ata.handle_frame(bytes([0xA5, 0x04, 0x01, 0x00]))   # OFFHOOK
    ata.write.assert_not_called()        # no ANSWER/CALL_ANNOUNCE frames
    assert ata.call is None
