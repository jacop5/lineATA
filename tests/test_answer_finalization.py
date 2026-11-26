"""TASK-0039: answer continuation vs shutdown while the SIP listener is still open.

Hypothesis under test: answer_incoming proceeds past prepare_media, parks at the
first ANSWER frame write; shutdown finalizes the INVITE (480, detach, close)
while sip_stop is STILL False (the pre-deregistration window); answer resumes and
may emit a 200 for the already-finalized INVITE.

Real SIPCall/answer_incoming/send_response/start_media; only the UDP/RTP socket
factory and the USB transport are faked, with Event barriers at those boundaries
(no whole-helper stubs, no real sockets/USB/network). Desired behavior: no 200/500
after the final 480. At baseline this is expected to FAIL if the hypothesis is
confirmed (defect pin, not a green characterization of bad behavior).
"""
import socket as real_socket
import threading
import types
from unittest.mock import Mock

import pytest

import bridge.ata as ata_mod
from bridge.ata import LinearATA, SIPCall

CFG = {'login': 't', 'password': 'x', 'domain': 'd', 'pbx_ip': '127.0.0.1',
       'pbx_port': 5060, 'local_ip': '127.0.0.1', 'sip_port': 5062, 'cold': True}

INVITE = ('INVITE sip:t@d SIP/2.0\r\n'
          'Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKinv1;rport\r\n'
          'From: <sip:69@d>;tag=caller1\r\n'
          'To: <sip:t@d>\r\n'
          'Call-ID: cid\r\nCSeq: 1 INVITE\r\n'
          'Contact: <sip:69@127.0.0.1:5060>\r\n'
          'Content-Type: application/sdp\r\nContent-Length: 54\r\n\r\n'
          'v=0\r\nc=IN IP4 127.0.0.1\r\ns=x\r\nm=audio 4000 RTP/AVP 0\r\n')


class FakeUDPSocket:
    instances = []

    def __init__(self, *a, **k):
        self.closed = False
        FakeUDPSocket.instances.append(self)

    def bind(self, addr):
        pass

    def settimeout(self, t):
        pass

    def getsockname(self):
        return ('127.0.0.1', 43000 + len(FakeUDPSocket.instances))

    def recvfrom(self, n):
        raise real_socket.timeout()

    def close(self):
        self.closed = True


class FakeSIPSock:
    """Records the chronological wire log of SIP responses."""

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
    """USB transport; first write parks on a barrier (the ANSWER boundary)."""

    def __init__(self):
        self.writes = []
        self.arm_barrier = False
        self.first_write_entered = threading.Event()
        self.first_write_release = threading.Event()

    def write(self, ep, data, timeout):
        if self.arm_barrier and not self.writes:
            self.first_write_entered.set()
            assert self.first_write_release.wait(2.0), "barrier timeout"
        self.writes.append(bytes(data))
        return len(data)


@pytest.fixture
def env(monkeypatch):
    FakeUDPSocket.instances = []
    monkeypatch.setattr(ata_mod, 'socket', types.SimpleNamespace(
        AF_INET=real_socket.AF_INET, SOCK_DGRAM=real_socket.SOCK_DGRAM,
        timeout=real_socket.timeout, socket=FakeUDPSocket))
    ata = LinearATA(dict(CFG))
    ata.log = lambda *a: None
    ata.sip_sock = FakeSIPSock()
    ata.sip_stop = threading.Event()
    ata.reg_event = threading.Event()
    ata.sip_local_port = 5062
    ata.usb_active = True           # runtime queue path: writes are submitted
    ata.usb_owner = threading.get_ident()
    ata.dev = FakeDev()
    return ata


def make_incoming(ata):
    """Admit through the REAL on_invite path with a valid INVITE (headers+SDP),
    so call_id/invite_cseq/tags match the wire message."""
    ata.handle_sip_message(INVITE, ('127.0.0.1', 5060))
    call = ata.call
    assert call is not None and call.incoming
    assert call.call_id == 'cid' and call.invite_cseq == '1'
    return call


def sip_codes(ata):
    return [m.split(b' ')[1].decode() for m in ata.sip_sock.sent if m.startswith(b'SIP/2.0')]


def invite_final_codes(ata):
    """Final (>=200) status codes of responses to the INVITE transaction only
    (CSeq: 1 INVITE), excluding provisional 100/180 and the 200 OK that answers
    the CANCEL transaction itself."""
    return [m.split(b' ')[1].decode() for m in ata.sip_sock.sent
            if m.startswith(b'SIP/2.0') and b'CSeq: 1 INVITE' in m
            and int(m.split(b' ')[1]) >= 200]


def test_answer_resumed_in_pre_sipstop_window_must_not_emit_200_after_480(env, monkeypatch):
    ata = env
    call = make_incoming(ata)  # real on_invite admission, consistent IDs/tags

    # Barrier AFTER the actual queue submission / after write() returns, outside
    # usb_state_lock (the wrapper calls the original write first, then parks), so
    # the main thread can still detach under the lock.
    orig_write = ata.write
    entered = threading.Event()
    release = threading.Event()
    state = {'n': 0}

    def write_then_park(*a, **k):
        orig_write(*a, **k)          # real queue submission happens first
        state['n'] += 1
        if state['n'] == 1:          # park at the first ANSWER-frame write
            entered.set()
            assert release.wait(2.0), "barrier timeout"

    monkeypatch.setattr(ata, 'write', write_then_park)

    done = {}

    def answer():
        try:
            ata.answer_incoming()
            done['ok'] = True
        except Exception as exc:
            done['exc'] = repr(exc)

    t = threading.Thread(target=answer, daemon=True)
    t.start()
    assert entered.wait(2.0)                        # answer parked after first queued write
    # Shutdown finalizes the INVITE while sip_stop is still False:
    ata.shutdown_requested.set()
    ata.stop.set()                                  # real shutdown sets stop before deregistration
    ata._detach_call()                              # sends the correlated final 480
    assert not ata.sip_stop.is_set()                 # listener still open (pre-deregistration window)
    codes_at_finalize = sip_codes(ata)
    assert '480' in codes_at_finalize, codes_at_finalize
    # correlate the final 480: original Call-ID/CSeq/Via + To-tag
    final480 = [m for m in ata.sip_sock.sent if m.startswith(b'SIP/2.0 480')][0].decode()
    assert 'Call-ID: cid' in final480 and 'CSeq: 1 INVITE' in final480
    assert 'branch=z9hG4bKinv1' in final480 and 'tag=' in final480
    assert call.closed                              # close chronology: closed before answer resumes
    release.set()                                   # answer resumes inside the window
    t.join(2.0)
    assert not t.is_alive()
    assert done == {"ok": True}, done
    codes = sip_codes(ata)
    print("wire:", codes, "| at finalize:", codes_at_finalize)
    # Desired: no 200/500 after the final 480 for the finalized INVITE.
    assert not any(c in ('200', '500') for c in codes), f"late SIP response after 480: {codes}"


def test_ordinary_answer_positive_control(env):
    ata = env
    call = make_incoming(ata)
    ata.answer_incoming()  # no shutdown: must answer normally
    codes = sip_codes(ata)
    assert '200' in codes and '480' not in codes, codes
    assert call.established and call.rtp is not None
    call.close()  # stop the media worker deterministically


def test_established_call_shutdown_sends_single_bye_no_200(env):
    ata = env
    call = make_incoming(ata)
    ata.answer_incoming()
    assert call.established
    ata.shutdown_requested.set()
    ata.stop.set()
    ata._detach_call()
    byes = [m for m in ata.sip_sock.sent if m.startswith(b'BYE ')]
    codes = sip_codes(ata)
    assert len(byes) == 1, byes
    assert codes.count('200') == 1 and '480' not in codes, codes


def test_answer_first_then_shutdown_finishes_200_before_bye(env):
    """Opposite order: answer wins the final decision; shutdown must not add a
    480/487, and the 200 must precede any BYE."""
    ata = env
    call = make_incoming(ata)
    ata.answer_incoming()  # completes the 200 under _final_lock first
    assert '200' in sip_codes(ata)
    ata.shutdown_requested.set()
    ata.stop.set()
    ata._detach_call()
    codes = sip_codes(ata)
    assert codes.count('200') == 1 and '480' not in codes and '487' not in codes, codes
    byes = [m for m in ata.sip_sock.sent if m.startswith(b'BYE ')]
    assert len(byes) == 1
    # chronology: the 200 precedes the BYE
    wire = [m.split(b'\r\n')[0] for m in ata.sip_sock.sent]
    assert any(m.startswith(b'SIP/2.0 200') for m in wire)
    assert wire.index(next(m for m in wire if m.startswith(b'SIP/2.0 200'))) < \
        next(i for i, m in enumerate(ata.sip_sock.sent) if m.startswith(b'BYE '))
    call.close()


CANCEL = ('CANCEL sip:t@d SIP/2.0\r\n'
          'Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKinv1;rport\r\n'
          'From: <sip:69@d>;tag=caller1\r\nTo: <sip:t@d>\r\n'
          'Call-ID: cid\r\nCSeq: 1 CANCEL\r\nContent-Length: 0\r\n\r\n')


def test_cancel_first_prevents_late_200(env):
    """Rejection-first: CANCEL's 487 wins the final decision; a resumed answer
    must emit no 200/500 and start no media."""
    ata = env
    call = make_incoming(ata)
    ata.handle_sip_message(CANCEL, ('127.0.0.1', 5060))  # real CANCEL path -> 487
    finals = invite_final_codes(ata)
    assert finals == ['487'], finals  # 487 on the INVITE; the 200-to-CANCEL is separate
    assert call.closed
    # a late answer attempt now finds the call finalized/closed
    ata.answer_incoming()
    finals = invite_final_codes(ata)
    assert finals == ['487'], finals  # no late 200/500 for the finalized INVITE
    assert not call.media and call.thread is None
