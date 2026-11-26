"""TASK-0054 independent packet-level DTMF wire acceptance (contract of TASK-0053).

Fake socket module + fake SIP socket only; REAL LinearATA admission
(on_invite), answer_incoming/invite, handle_frame, start_media/media_loop,
send_dtmf and sdp_local run unmodified — no entire media_loop/answer/
send_dtmf replacement, no private lock/encoder names. RTP is inspected at the
call.rtp.sendto boundary (packet level).

Public contract under test:
- sdp_local(addr, port, dtmf_pt=None, dtmf_events=None): default stays
  PCMU-only; with dtmf_pt/dtmf_events it adds that telephone-event PT.
- outgoing default offer carries PT 101 telephone-event/8000 fmtp 0-15;
  incoming may negotiate e.g. PT 110; answer uses offered PT + fmtp
  intersection only; wrong-clock/malformed event params disable DTMF but never
  reject the PCMU call; dtmf_mode='inband' advertises no event PT.
- SIPCall.send_dtmf(digit)->bool: 0-9*#A-D, only when established/media-active/
  not closed/cancelled/shutting down and event negotiated; bounded queue;
  invalid/unnegotiated/closed -> False.
- RTP: 4-byte event payload, negotiated PT, marker only on first packet, E bit
  on the final 3 copies, volume 10, fixed 800-unit (100 ms) nominal duration,
  durations 160/320/480/640/800 then 800x3 (8 packets), one RTP timestamp per
  digit event (distinct for repeated digits, >=560 units = 70 ms apart), one
  global seq + shared SSRC across PCMU and events, PCMU byte-exact before/
  during/after, 20 ms pacing, in-call USB 0xA4 routes to send_dtmf without
  touching pre-call dialing or originating calls, close cancels queued events.
"""
import socket as real_socket
import struct
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bridge.ata as ata_mod
from bridge.ata import LinearATA, SIPCall, sdp_local

CFG = {'login': 't', 'password': 'x', 'domain': 'd', 'pbx_ip': '127.0.0.1',
       'pbx_port': 5060, 'local_ip': '127.0.0.1', 'sip_port': 5062, 'cold': True}
ADDR = ('127.0.0.1', 5060)

DIGIT_EVENT = {str(d): d for d in range(10)}
DIGIT_EVENT.update({'*': 10, '#': 11, 'A': 12, 'B': 13, 'C': 14, 'D': 15})


def invite_sdp(pt_lines, rtpmaps, fmtp=None):
    body = ('v=0\r\nc=IN IP4 127.0.0.1\r\ns=x\r\n'
            f'm=audio 4000 RTP/AVP {pt_lines}\r\n' + rtpmaps +
            'a=ptime:20\r\n')
    if fmtp:
        body += fmtp
    return body


INVITE_PCMU_DTMF110 = (
    'INVITE sip:t@d SIP/2.0\r\n'
    'Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKinv1;rport\r\n'
    'From: <sip:69@d>;tag=caller1\r\nTo: <sip:t@d>\r\n'
    'Call-ID: cid\r\nCSeq: 1 INVITE\r\n'
    'Contact: <sip:69@127.0.0.1:5060>\r\n'
    'Content-Type: application/sdp\r\n'
    f'Content-Length: {len(invite_sdp("0 110", "a=rtpmap:0 PCMU/8000\\r\\na=rtpmap:110 telephone-event/8000\\r\\n", "a=fmtp:110 0-9\\r\\n"))}\r\n\r\n'
    + invite_sdp('0 110', 'a=rtpmap:0 PCMU/8000\r\na=rtpmap:110 telephone-event/8000\r\n',
                 'a=fmtp:110 0-9\r\n'))

INVITE_PCMU_DTMF_WRONGCLOCK = (
    'INVITE sip:t@d SIP/2.0\r\n'
    'Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKinv2;rport\r\n'
    'From: <sip:69@d>;tag=caller1\r\nTo: <sip:t@d>\r\n'
    'Call-ID: cid2\r\nCSeq: 1 INVITE\r\n'
    'Contact: <sip:69@127.0.0.1:5060>\r\n'
    'Content-Type: application/sdp\r\n'
    f'Content-Length: {len(invite_sdp("0 110", "a=rtpmap:0 PCMU/8000\\r\\na=rtpmap:110 telephone-event/9000\\r\\n"))}\r\n\r\n'
    + invite_sdp('0 110', 'a=rtpmap:0 PCMU/8000\r\na=rtpmap:110 telephone-event/9000\r\n'))


class FakeUDPSocket:
    """RTP/UDP fake: records every sendto payload (packet-level inspection)."""

    def __init__(self, *a, **k):
        self.sent = []
        self.closed = False

    def bind(self, addr):
        pass

    def settimeout(self, t):
        pass

    def getsockname(self):
        return ('127.0.0.1', 43000 + id(self) % 1000)

    def sendto(self, data, addr):
        self.sent.append(bytes(data))

    def recvfrom(self, n):
        raise real_socket.timeout()

    def close(self):
        self.closed = True


class FakeSIPSock:
    def __init__(self):
        self.sent = []
        self.closed = False

    def sendto(self, data, addr):
        if self.closed:
            raise OSError('closed')
        self.sent.append(data if isinstance(data, bytes) else data.encode())

    def recvfrom(self, n):
        raise real_socket.timeout()

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
    ata.write = Mock()                      # USB transport fake; count only
    return ata


def make_incoming(ata, invite=INVITE_PCMU_DTMF110):
    ata.handle_sip_message(invite, ADDR)
    call = ata.call
    assert call is not None and call.incoming
    return call


def answer_call(ata, call):
    ata.answer_incoming()
    assert call.established, 'answer did not establish'
    return call


def rtp_packets(call):
    return [parse_rtp(d) for d in call.rtp.sent]


def parse_rtp(d):
    return dict(pt=d[1] & 0x7F, marker=bool(d[1] & 0x80),
                seq=struct.unpack('!H', d[2:4])[0],
                ts=struct.unpack('!I', d[4:8])[0],
                ssrc=struct.unpack('!I', d[8:12])[0], payload=d[12:])


def event_of(pkt):
    # RFC 4733 sec 2.3: byte0 = event id, byte1 = E|R|volume, byte2-3 = duration
    p = pkt['payload']
    assert len(p) == 4, f'event payload must be 4 bytes, got {len(p)}'
    return dict(ev=p[0] & 0x7F, e=bool(p[1] & 0x80), r=bool(p[1] & 0x40),
                vol=p[1] & 0x3F, dur=struct.unpack('!H', p[2:4])[0])


def wait_until(cond, timeout=5.0, step=0.005):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def sip_responses(ata, code):
    return [m for m in ata.sip_sock.sent
            if m.startswith(f'SIP/2.0 {code} '.encode())]


def stop_call(call):
    call.close()
    if call.thread is not None:
        call.thread.join(2.0)
        assert not call.thread.is_alive()


# ---------------------------------------------------------------- SDP helpers
def test_sdp_local_default_remains_pcmu_only():
    plain = sdp_local('127.0.0.1', 24000)
    assert 'telephone-event' not in plain and 'm=audio 24000 RTP/AVP 0\r\n' in plain
    with_pt = sdp_local('127.0.0.1', 24000, dtmf_pt=101, dtmf_events=set(range(16)))
    assert 'a=rtpmap:101 telephone-event/8000' in with_pt
    assert 'a=fmtp:101 0-15' in with_pt
    assert 'm=audio 24000 RTP/AVP 0 101\r\n' in with_pt or ' 0 101' in with_pt
    assert 'a=rtpmap:0 PCMU/8000' in with_pt   # PCMU preserved


# ------------------------------------------------------- outgoing negotiation
def make_outgoing(ata, answer_body):
    call = SIPCall(ata, '123')
    call.sock = ata.sip_sock
    call.remote = ADDR
    ata.call = call
    ata.invite_transactions[call.call_id] = call
    orig_send = call.send

    def send_spy(data, addr):
        orig_send(data, addr)
        req = data.decode() if isinstance(data, bytes) else data
        if req.startswith('INVITE') and 'Authorization' not in req \
                and not any(m.startswith(b'SIP/2.0 200') for m in ata.sip_sock.sent):
            call.responses.put((respond_ok(data, answer_body), ADDR))

    call.send = send_spy
    return call                                # real start_media/media_loop runs


def respond_ok(request, body):
    req = request.decode() if isinstance(request, bytes) else request
    head = req.split('\r\n\r\n', 1)[0]
    keep = {}
    for line in head.split('\r\n')[1:]:
        name = line.split(':', 1)[0].strip().lower()
        if name in ('via', 'from', 'to', 'call-id', 'cseq'):
            keep[name] = line
    to = keep['to'] + ';tag=remote1'
    out = ['SIP/2.0 200 OK', keep['via'], keep['from'], to, keep['call-id'],
           keep['cseq'], 'Contact: <sip:pbx@127.0.0.1:5060>',
           'Content-Type: application/sdp', f'Content-Length: {len(body)}']
    return '\r\n'.join(out) + '\r\n\r\n' + body


ANSWER_101 = ('v=0\r\nc=IN IP4 127.0.0.1\r\ns=x\r\nm=audio 4000 RTP/AVP 0 101\r\n'
              'a=rtpmap:0 PCMU/8000\r\na=rtpmap:101 telephone-event/8000\r\n'
              'a=fmtp:101 0-15\r\na=ptime:20\r\n')
ANSWER_105_UNOFFERED = ('v=0\r\nc=IN IP4 127.0.0.1\r\ns=x\r\nm=audio 4000 RTP/AVP 0 105\r\n'
                        'a=rtpmap:0 PCMU/8000\r\na=rtpmap:105 telephone-event/8000\r\n'
                        'a=ptime:20\r\n')


def test_outgoing_offer_101_and_negotiated_event_pt(env):
    ata = env
    call = make_outgoing(ata, ANSWER_101)
    try:
        assert call.invite() is True
        invites = [m.decode() for m in ata.sip_sock.sent if m.startswith(b'INVITE')]
        assert any('a=rtpmap:101 telephone-event/8000' in i for i in invites), \
            'default outgoing offer must include PT 101 telephone-event/8000'
        assert call.send_dtmf('5') is True, 'negotiated 101 must accept digits'
        assert wait_until(lambda: sum(1 for p in rtp_packets(call) if p['pt'] == 101) >= 8)
        evs = [p for p in rtp_packets(call) if p['pt'] == 101]
        assert all(event_of(p)['ev'] == 5 for p in evs)
    finally:
        stop_call(call)


def test_outgoing_unoffered_event_pt_preserves_pcmu_call(env):
    ata = env
    call = make_outgoing(ata, ANSWER_105_UNOFFERED)
    try:
        assert call.invite() is True, 'PCMU call must still establish'
        assert call.send_dtmf('5') is False, 'unoffered event PT must not negotiate'
        ata.push_usb_audio(bytes(range(160)))
        assert wait_until(lambda: any(p['pt'] == 0 for p in rtp_packets(call)))
        assert not any(p['pt'] != 0 for p in rtp_packets(call)), \
            'no event packets without negotiation'
    finally:
        stop_call(call)


def test_inband_mode_advertises_no_event_pt(env):
    ata = env
    ata.cfg['dtmf_mode'] = 'inband'
    call = make_outgoing(ata, ANSWER_101)
    try:
        assert call.invite() is True
        invites = [m.decode() for m in ata.sip_sock.sent if m.startswith(b'INVITE')]
        assert all('telephone-event' not in i for i in invites), \
            'inband mode must not advertise an event PT'
        assert call.send_dtmf('5') is False
    finally:
        stop_call(call)


# ------------------------------------------------------- incoming negotiation
def test_incoming_offer_110_answer_uses_offered_pt_and_intersection(env):
    ata = env
    call = make_incoming(ata)
    try:
        answer_call(ata, call)
        ok200 = sip_responses(ata, '200')
        assert ok200, 'no 200 answer sent'
        body = ok200[-1].decode().split('\r\n\r\n', 1)[1]
        assert 'a=rtpmap:110 telephone-event/8000' in body, body
        assert ' 110' in body.split('m=audio', 1)[1].split('\r\n', 1)[0], body
        assert 'a=rtpmap:0 PCMU/8000' in body, 'PCMU must be preserved'
        fmtp = [l for l in body.split('\r\n') if l.lower().startswith('a=fmtp:110')]
        assert fmtp, 'answer must carry the fmtp intersection'
        offered = set('0123456789')
        answered = set()
        for part in fmtp[0].split(' ', 1)[1].replace(' ', '').split(','):
            if '-' in part:
                a, b = part.split('-')
                answered.update(str(x) for x in range(int(a), int(b) + 1))
            elif part:
                answered.add(part)
        assert answered and answered <= offered, \
            f'answer fmtp must be the intersection only, got {answered}'
        assert call.send_dtmf('5') is True
        assert call.send_dtmf('*') is False, 'event 10 is outside the 0-9 peer set'
    finally:
        stop_call(call)


def test_incoming_wrong_clock_event_preserves_pcmu(env):
    ata = env
    call = make_incoming(ata, INVITE_PCMU_DTMF_WRONGCLOCK)
    try:
        answer_call(ata, call)               # must NOT 488/reject
        assert call.send_dtmf('5') is False, 'wrong clock rate must disable DTMF'
        ata.push_usb_audio(bytes([0xFF] * 160))
        assert wait_until(lambda: any(p['pt'] == 0 for p in rtp_packets(call)))
    finally:
        stop_call(call)


# ------------------------------------------------------------- USB 0xA4 routing
def test_in_call_a4_routes_to_send_dtmf_pre_call_unaffected(env):
    ata = env
    call = make_incoming(ata)
    answer_call(ata, call)
    try:
        spy = Mock(wraps=call.send_dtmf)
        call.send_dtmf = spy
        ata.handle_frame(bytes([0xA4, 0x04, ord('5'), 0x00]))   # real frame path
        assert spy.call_count == 1 and spy.call_args[0][0] == '5'
        assert ata.call is call, 'in-call digit must not originate another call'
        assert ata.dialing == '', 'in-call digit must not alter dialing'
    finally:
        stop_call(call)
    # pre-call: hook on, no call -> digits still dial, send_dtmf untouched
    ata.call = None
    ata.dialing = ''
    ata.ports[1]['hook'] = True
    ata.handle_frame(bytes([0xA4, 0x04, ord('7'), 0x00]))
    assert ata.dialing == '7' and ata.call is None


# ------------------------------------------------------------- RTP wire shape
def test_event_packet_shape_seq_ssrc_pcmu_exact(env):
    ata = env
    call = make_incoming(ata)
    answer_call(ata, call)
    try:
        pcm_stream = bytes((i * 7) % 256 for i in range(160 * 6))   # 6 known frames
        for i in range(6):
            ata.push_usb_audio(pcm_stream[i * 160:(i + 1) * 160])
        assert call.send_dtmf('5') is True
        assert wait_until(lambda: sum(1 for p in rtp_packets(call) if p['pt'] == 110) >= 8,
                          timeout=6.0), 'timed out waiting for 8 event packets'
        pkts = rtp_packets(call)
        evs = [p for p in pkts if p['pt'] == 110]
        pcmu = [p for p in pkts if p['pt'] == 0]
        assert len(evs) == 8, f'expected 8 event packets (5 updates + 3 end), got {len(evs)}'
        # one global sequence across PCMU + events, shared SSRC
        seqs = [p['seq'] for p in pkts]
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), 'RTP seq must be global/dense'
        assert len({p['ssrc'] for p in pkts}) == 1, 'PCMU and events must share one SSRC'
        # event shape: PT, 4-byte payload, ev id, volume 10, marker first only,
        # E on the last 3 only, durations 160..800 then 800x3, one ts per event
        assert all(p['pt'] == 110 for p in evs)
        e = [event_of(p) for p in evs]
        assert all(x['ev'] == 5 and x['vol'] == 10 for x in e)
        assert [x['dur'] for x in e] == [160, 320, 480, 640, 800, 800, 800, 800]
        assert [p['marker'] for p in evs] == [True] + [False] * 7
        assert [x['e'] for x in e] == [False] * 5 + [True] * 3
        assert len({p['ts'] for p in evs}) == 1, 'one RTP timestamp per digit event'
        # PCMU byte-exact before/during/after events
        assert b''.join(p['payload'] for p in pcmu) == pcm_stream[:len(pcmu) * 160]
        assert len(pcmu) >= 1
    finally:
        stop_call(call)


def test_repeated_digit_distinct_timestamp_and_min_gap(env):
    ata = env
    call = make_incoming(ata)
    answer_call(ata, call)
    try:
        assert call.send_dtmf('5') is True
        assert wait_until(lambda: sum(1 for p in rtp_packets(call) if p['pt'] == 110) >= 8,
                          timeout=6.0)
        assert call.send_dtmf('5') is True     # same digit again
        assert wait_until(lambda: sum(1 for p in rtp_packets(call) if p['pt'] == 110) >= 16,
                          timeout=6.0), 'repeated digit produced no second event group'
        evs = [p for p in rtp_packets(call) if p['pt'] == 110]
        ts1 = evs[0]['ts']
        ts2 = evs[8]['ts']
        assert ts1 != ts2, 'repeated identical digit must use a new RTP timestamp'
        assert (ts2 - ts1) & 0xFFFFFFFF >= 560, \
            f'min interdigit gap 70 ms = 560 units at 8000 Hz, got {(ts2 - ts1) & 0xFFFFFFFF}'
    finally:
        stop_call(call)


# ------------------------------------------------------------------ queue gates
INVITE_PCMU_DTMF110_FULL = (
    'INVITE sip:t@d SIP/2.0\r\n'
    'Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKinv3;rport\r\n'
    'From: <sip:69@d>;tag=caller1\r\nTo: <sip:t@d>\r\n'
    'Call-ID: cid3\r\nCSeq: 1 INVITE\r\n'
    'Contact: <sip:69@127.0.0.1:5060>\r\n'
    'Content-Type: application/sdp\r\n'
    f'Content-Length: {len(invite_sdp("0 110", "a=rtpmap:0 PCMU/8000\\r\\na=rtpmap:110 telephone-event/8000\\r\\n", "a=fmtp:110 0-15\\r\\n"))}\r\n\r\n'
    + invite_sdp('0 110', 'a=rtpmap:0 PCMU/8000\r\na=rtpmap:110 telephone-event/8000\r\n',
                 'a=fmtp:110 0-15\r\n'))


def test_send_dtmf_gates(env):
    ata = env
    call = make_incoming(ata, INVITE_PCMU_DTMF110_FULL)   # peer supports 0-15
    # not established yet -> refuse
    assert call.send_dtmf('5') is False
    answer_call(ata, call)
    try:
        assert call.send_dtmf('x') is False, 'invalid digit must be refused'
        assert call.send_dtmf('') is False and call.send_dtmf('55') is False
        # STRICT API boundary per lead: only 0-9*#A-D; lowercase is invalid.
        # (Current source normalizes via .upper() — reported as a source
        # mismatch, this desired assertion is intentionally kept strict.)
        assert call.send_dtmf('a') is False, 'lowercase must be refused (0-9*#A-D only)'
        for d in '0123456789*#ABCD':
            assert call.send_dtmf(d) is True, f'digit {d!r} must queue when negotiated'
    finally:
        stop_call(call)
    assert call.send_dtmf('5') is False, 'closed call must refuse digits'
    # unnegotiated call refuses
    ata2 = env
    call2 = make_incoming(ata2, INVITE_PCMU_DTMF_WRONGCLOCK)
    answer_call(ata2, call2)
    try:
        assert call2.send_dtmf('5') is False
    finally:
        stop_call(call2)


def test_close_cancels_queued_events(env):
    ata = env
    call = make_incoming(ata)
    answer_call(ata, call)
    assert call.send_dtmf('5') is True
    call.close()
    if call.thread is not None:
        call.thread.join(2.0)
        assert not call.thread.is_alive()
    events_at_close = sum(1 for p in rtp_packets(call) if p['pt'] == 110)
    time.sleep(0.5)                              # bounded observation window
    events_after = sum(1 for p in rtp_packets(call) if p['pt'] == 110)
    assert events_after == events_at_close, \
        'no event packets may be emitted after close'
    assert call.send_dtmf('5') is False
