"""TASK-0053: negotiated outbound RFC 4733 DTMF (bridge/dtmf.py + bridge/ata.py).

Deterministic, offline: pure DtmfSchedule packet-state tests, SDP
negotiation fixtures, send_dtmf queue/close gates, and a fake-clock
media_loop run asserting global RTP seq/SSRC/timestamp semantics with PCMU
interleaved unchanged. No real sockets, hardware, network or PBX.
"""
import socket
import struct
from unittest.mock import Mock

import pytest

import bridge.ata as ata_mod
from bridge.ata import LinearATA, SIPCall, parse_sdp, parse_sdp_dtmf, sdp_local
from bridge.dtmf import (DTMF_EVENTS, SUPPORTED_EVENTS, DtmfSchedule,
                         build_event_packet, digit_to_event, format_events,
                         parse_events_list, SCHEDULE)

CFG = {'login': 't', 'password': 'x', 'domain': 'd', 'pbx_ip': '127.0.0.1',
       'pbx_port': 5060, 'local_ip': '127.0.0.1', 'sip_port': 5062, 'cold': True,
       'dtmf_mode': 'rfc4733'}


def offer(body_pts='0 101', rtpmap='a=rtpmap:101 telephone-event/8000\r\n',
          fmtp='a=fmtp:101 0-15\r\n', extra=''):
    return ('v=0\r\no=- 1 1 IN IP4 10.0.0.1\r\ns=x\r\nc=IN IP4 192.0.2.3\r\n'
            't=0 0\r\n'
            f'm=audio 5000 RTP/AVP {body_pts}\r\n'
            'a=rtpmap:0 PCMU/8000\r\n' + rtpmap + fmtp + extra)


# ---------------------------------------------------------------- pure helper

def test_digit_to_event_mapping_and_invalid():
    assert [digit_to_event(c) for c in '0123456789'] == list(range(10))
    assert digit_to_event('*') == 10
    assert digit_to_event('#') == 11
    assert [digit_to_event(c) for c in 'ABCD'] == [12, 13, 14, 15]
    assert digit_to_event('a') is None  # strict USB/API digit alphabet
    for bad in ('', '12', 'x', ' ', None, 5):
        assert digit_to_event(bad) is None


def test_build_event_packet_layout():
    # sec 2.3 Fig. 1: event | E|R|volume | duration(!H); R MUST be 0
    assert build_event_packet(5, False, 160).hex() == '050a00a0'
    assert build_event_packet(11, True, 800, volume=10).hex() == '0b8a0320'
    assert build_event_packet(0, True, 0, volume=0).hex() == '008000 00'.replace(' ', '')
    with pytest.raises(ValueError):
        build_event_packet(256, False, 10)
    with pytest.raises(ValueError):
        build_event_packet(1, False, 0x10000)


def test_parse_events_list():
    assert parse_events_list('0-15') == set(range(16))
    assert parse_events_list('0-15,66,70') == set(range(16)) | {66, 70}
    assert parse_events_list('5') == {5}
    assert parse_events_list('0-2,5') == {0, 1, 2, 5}
    for bad in ('', 'abc', '0-', '5-2', '0-15, x', '256', '0-300'):
        assert parse_events_list(bad) is None


def test_format_events():
    assert format_events(set(range(16))) == '0-15'
    assert format_events({0, 1, 2, 5}) == '0-2,5'
    assert format_events({7}) == '7'


def test_schedule_single_event_sequence():
    s = DtmfSchedule()
    assert s.enqueue(digit_to_event('5'))
    out = []
    now = 1000.0
    for i in range(len(SCHEDULE)):
        r = s.poll(now + i * 0.020)
        assert r is not None, i
        out.append(r)
    # durations 160/320/480/640/800 with E=0, then E=1 duration 800 three times
    assert [(d, e) for _e, e, d, _f in out] == [(d, e) for d, e in SCHEDULE]
    assert [f for _e, _end, _d, f in out] == [True] + [False] * (len(SCHEDULE) - 1)
    assert all(e == 5 for e, _end, _d, _f in out)
    assert s.poll(now + len(SCHEDULE) * 0.020) is None  # event finished
    assert s.pending() == 0


def test_schedule_at_most_one_update_per_poll_and_no_burst():
    s = DtmfSchedule()
    s.enqueue(3)
    now = 500.0
    first = s.poll(now)
    assert first == (3, False, 160, True)
    assert s.poll(now) is None               # same tick: no second update
    assert s.poll(now + 0.010) is None       # before next 20 ms tick
    second = s.poll(now + 0.020)
    assert second == (3, False, 320, False)
    # scheduler stall: one packet only, schedule resumes from now (no burst)
    r = s.poll(now + 1.000)
    assert r == (3, False, 480, False)
    assert s.poll(now + 1.000) is None
    assert s.poll(now + 1.010) is None


def test_schedule_interdigit_gap_and_repeated_digits():
    s = DtmfSchedule()
    t0 = 2000.0
    s.enqueue(5)
    s.enqueue(5)  # identical consecutive digit must still get its own event
    # first event occupies 8 ticks (0..140 ms); nominal end = t0 + 100 ms;
    # gap 70 ms -> next start no earlier than t0 + 180 ms on the 20 ms grid
    seen_starts = []
    now = t0
    while s.pending() and now < t0 + 1.0:
        r = s.poll(now)
        if r is not None and r[3]:
            seen_starts.append(now)
        now += 0.020
    assert len(seen_starts) == 2
    assert seen_starts[1] - seen_starts[0] >= 0.180 - 1e-9
    assert seen_starts[0] != seen_starts[1]  # distinct event start times


def test_schedule_queue_bound_and_clear():
    s = DtmfSchedule()
    for i in range(32):
        assert s.enqueue(i % 16)
    assert s.enqueue(0) is False  # bounded queue overflow
    s.clear()
    assert s.pending() == 0
    assert s.poll(9999.0) is None


# ---------------------------------------------------------------- SDP

def test_sdp_local_default_is_unchanged_pcmu_only():
    body = sdp_local('127.0.0.1', 24000)
    assert 'm=audio 24000 RTP/AVP 0\r\n' in body
    assert 'telephone-event' not in body
    assert body.endswith('a=sendrecv\r\n')


def test_sdp_local_with_dtmf_pt():
    body = sdp_local('127.0.0.1', 24000, dtmf_pt=101, dtmf_events=SUPPORTED_EVENTS)
    assert 'm=audio 24000 RTP/AVP 0 101\r\n' in body
    assert 'a=rtpmap:101 telephone-event/8000\r\n' in body
    assert 'a=fmtp:101 0-15\r\n' in body
    assert 'a=rtpmap:0 PCMU/8000\r\n' in body
    subset = sdp_local('127.0.0.1', 24000, dtmf_pt=101, dtmf_events={0, 1, 2, 5})
    assert 'a=fmtp:101 0-2,5\r\n' in subset


def test_parse_sdp_two_tuple_preserved():
    assert parse_sdp(sdp_local('127.0.0.1', 24000)) == ('127.0.0.1', 24000)
    assert parse_sdp(offer()) == ('192.0.2.3', 5000)
    assert parse_sdp(offer(body_pts='8')) == (None, None)  # PCMU-only acceptance


def test_parse_sdp_dtmf_incoming_offer_variants():
    assert parse_sdp_dtmf(offer()) == (101, frozenset(range(16)))
    # absent fmtp defaults to 0-15
    assert parse_sdp_dtmf(offer(fmtp='')) == (101, frozenset(range(16)))
    # explicit set is intersected with ours
    assert parse_sdp_dtmf(offer(fmtp='a=fmtp:101 0-9,66\r\n')) == (101, frozenset(range(10)))
    # wrong clock disables DTMF but the PCMU call stays valid
    body = offer(rtpmap='a=rtpmap:101 telephone-event/16000\r\n')
    assert parse_sdp_dtmf(body) is None
    assert parse_sdp(body) == ('192.0.2.3', 5000)
    # PT outside the dynamic range is not accepted on an incoming offer
    assert parse_sdp_dtmf(offer(body_pts='0 34',
                                rtpmap='a=rtpmap:34 telephone-event/8000\r\n',
                                fmtp='a=fmtp:34 0-15\r\n')) is None
    # contradictory mapping for the same PT disables it
    assert parse_sdp_dtmf(offer(extra='a=rtpmap:101 red/8000/1\r\n')) is None
    # disjoint event set disables DTMF, PCMU survives
    body = offer(fmtp='a=fmtp:101 66,70\r\n')
    assert parse_sdp_dtmf(body) is None
    assert parse_sdp(body) == ('192.0.2.3', 5000)
    # malformed fmtp disables DTMF, PCMU survives
    body = offer(fmtp='a=fmtp:101 bogus\r\n')
    assert parse_sdp_dtmf(body) is None
    assert parse_sdp(body) == ('192.0.2.3', 5000)
    # missing rtpmap for the offered PT
    assert parse_sdp_dtmf(offer(rtpmap='')) is None


def test_parse_sdp_dtmf_answer_requires_offered_pt():
    # answering our offer: only PT 101 counts; an unoffered 102 is ignored
    assert parse_sdp_dtmf(offer(), offer_pt=101) == (101, frozenset(range(16)))
    body = offer(body_pts='0 102', rtpmap='a=rtpmap:102 telephone-event/8000\r\n',
                 fmtp='a=fmtp:102 0-15\r\n')
    assert parse_sdp_dtmf(body, offer_pt=101) is None
    assert parse_sdp_dtmf(offer(body_pts='0'), offer_pt=101) is None


def test_dtmf_in_a_later_media_section_is_not_used():
    body = ('v=0\r\nc=IN IP4 192.0.2.3\r\nt=0 0\r\n'
            'm=audio 5000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n'
            'm=audio 5002 RTP/AVP 101\r\na=rtpmap:101 telephone-event/8000\r\n'
            'a=fmtp:101 0-15\r\n')
    assert parse_sdp_dtmf(body) is None
    assert parse_sdp(body) == ('192.0.2.3', 5000)


# ---------------------------------------------------------------- send_dtmf gates

def make_call(established=True, media=True, negotiated=True, **cfg_over):
    cfg = dict(CFG)
    cfg.update(cfg_over)
    ata = LinearATA(cfg)
    ata.log = Mock()
    call = SIPCall(ata, '123')
    call.established = established
    call.media = media
    if negotiated:
        call.dtmf_pt = 101
        call.dtmf_events = frozenset(SUPPORTED_EVENTS)
    return ata, call


def test_send_dtmf_happy_path_queues_event():
    _ata, call = make_call()
    assert call.send_dtmf('5') is True
    assert call.dtmf_schedule.pending() == 1
    assert call.send_dtmf('#') is True


def test_send_dtmf_invalid_and_unnegotiated():
    _ata, call = make_call()
    assert call.send_dtmf('x') is False
    assert call.send_dtmf('') is False
    assert call.send_dtmf('12') is False
    assert call.dtmf_schedule.pending() == 0
    _ata2, call2 = make_call(negotiated=False)
    assert call2.send_dtmf('5') is False
    # negotiated PT but peer event set disjoint from the digit
    _a3, call3 = make_call()
    call3.dtmf_events = frozenset({0, 1})
    assert call3.send_dtmf('5') is False
    assert call3.send_dtmf('1') is True


def test_send_dtmf_state_gates():
    ata, call = make_call()
    call.closed = True
    assert call.send_dtmf('5') is False
    _a, call = make_call()
    call.cancelled = True
    assert call.send_dtmf('5') is False
    _a, call = make_call(established=False)
    assert call.send_dtmf('5') is False
    _a, call = make_call(media=False)
    assert call.send_dtmf('5') is False
    ata, call = make_call()
    ata.shutdown_requested.set()
    assert call.send_dtmf('5') is False
    ata2, call2 = make_call()
    ata2.stop.set()
    assert call2.send_dtmf('5') is False


def test_send_dtmf_queue_overflow_returns_false():
    _ata, call = make_call()
    for _ in range(32):
        assert call.send_dtmf('1') is True
    assert call.send_dtmf('1') is False
    assert call.dtmf_schedule.pending() == 32


def test_close_and_hangup_discard_queue():
    _ata, call = make_call()
    assert call.send_dtmf('5') is True
    call.close()
    assert call.dtmf_schedule.pending() == 0
    assert call.send_dtmf('5') is False
    _a2, call2 = make_call()
    assert call2.send_dtmf('5') is True
    call2.hangup()
    assert call2.dtmf_schedule.pending() == 0


# ---------------------------------------------------------------- media_loop wire

class Clock:
    value = 0.0

    def now(self):
        return self.value


def run_media(call, clock, seconds=1.0):
    """Run the REAL media_loop under a fake clock; return captured packets."""
    sent = []
    timeout = [.005]

    def sendto(data, addr):
        sent.append(data)

    def receive(_):
        clock.value += timeout[0] + .0005
        if clock.value >= seconds:
            call.media = False
        raise socket.timeout()

    call.rtp = Mock()
    call.rtp.sendto.side_effect = sendto
    call.rtp.settimeout.side_effect = lambda v: timeout.__setitem__(0, v)
    call.rtp.recvfrom.side_effect = receive
    call.media_loop()
    return sent


def parse_rtp(pkt):
    b0, b1, seq, ts, ssrc = struct.unpack('!BBHII', pkt[:12])
    return {'m': bool(b1 & 0x80), 'pt': b1 & 0x7F, 'seq': seq, 'ts': ts,
            'ssrc': ssrc, 'payload': pkt[12:]}


def test_media_loop_interleaves_event_with_shared_seq_ssrc(monkeypatch):
    clock = Clock()
    monkeypatch.setattr('bridge.ata.time.monotonic', clock.now)
    ata = LinearATA(dict(CFG))
    ata.log = Mock()
    ata.take_usb_audio = Mock(return_value=b'\xff' * 160)
    call = SIPCall(ata, 'synthetic')
    call.remote_ip, call.remote_rtp = '127.0.0.1', 24000
    call.media = True
    call.established = True
    call.dtmf_pt = 101
    call.dtmf_events = frozenset(SUPPORTED_EVENTS)
    assert call.send_dtmf('5') is True
    sent = run_media(call, clock, seconds=0.6)
    pkts = [parse_rtp(p) for p in sent]
    ev = [p for p in pkts if p['pt'] == 101]
    pcm = [p for p in pkts if p['pt'] == 0]
    assert pcm, 'PCMU stream must be unchanged'
    assert all(len(p['payload']) == 160 for p in pcm)
    # full 8-packet event plan for digit 5 (event code 5)
    assert len(ev) == 8
    assert [struct.unpack('!B', p['payload'][0:1])[0] for p in ev] == [5] * 8
    assert [bool(p['payload'][1] & 0x80) for p in ev] == [False] * 5 + [True] * 3
    assert [p['payload'][1] & 0x3F for p in ev] == [10] * 8      # volume, R=0
    assert [int.from_bytes(p['payload'][2:4], 'big') for p in ev] == \
        [160, 320, 480, 640, 800, 800, 800, 800]
    assert [p['m'] for p in ev] == [True] + [False] * 7           # marker first only
    # one RTP owner: shared SSRC, strictly consecutive seq across BOTH PTs
    assert len({p['ssrc'] for p in pkts}) == 1
    seqs = [p['seq'] for p in pkts]
    assert all((b - a) & 0xFFFF == 1 for a, b in zip(seqs, seqs[1:]))
    # constant timestamp per event
    assert len({p['ts'] for p in ev}) == 1


def test_media_loop_repeated_digit_distinct_event_ts(monkeypatch):
    clock = Clock()
    monkeypatch.setattr('bridge.ata.time.monotonic', clock.now)
    ata = LinearATA(dict(CFG))
    ata.log = Mock()
    ata.take_usb_audio = Mock(return_value=b'\xff' * 160)
    call = SIPCall(ata, 'synthetic')
    call.remote_ip, call.remote_rtp = '127.0.0.1', 24000
    call.media = True
    call.established = True
    call.dtmf_pt = 101
    call.dtmf_events = frozenset(SUPPORTED_EVENTS)
    assert call.send_dtmf('7') is True
    assert call.send_dtmf('7') is True
    sent = run_media(call, clock, seconds=1.0)
    ev = [q for q in (parse_rtp(p) for p in sent) if q['pt'] == 101]
    firsts = [p for p in ev if p['m']]
    assert len(firsts) == 2
    assert firsts[0]['ts'] != firsts[1]['ts']  # distinct timestamps
    assert len(ev) == 16


def test_media_loop_no_dtmf_when_unnegotiated(monkeypatch):
    clock = Clock()
    monkeypatch.setattr('bridge.ata.time.monotonic', clock.now)
    ata = LinearATA(dict(CFG))
    ata.log = Mock()
    ata.take_usb_audio = Mock(return_value=b'\xff' * 160)
    call = SIPCall(ata, 'synthetic')
    call.remote_ip, call.remote_rtp = '127.0.0.1', 24000
    call.media = True
    call.established = True
    assert call.send_dtmf('5') is False  # dtmf_pt None: ignored
    sent = run_media(call, clock, seconds=0.2)
    assert sent and all(parse_rtp(p)['pt'] == 0 for p in sent)


def test_handle_frame_routes_in_call_digit_to_send_dtmf():
    ata, call = make_call()
    ata.call = call
    call.send_dtmf = Mock(return_value=True)
    ata.handle_frame(bytes([0xA4, 0x04, 0x35, 0x00]))
    call.send_dtmf.assert_called_once_with('5')
    # pre-call dialing path is untouched
    ata.call = None
    ata.ports[1]['hook'] = True
    ata.write = Mock()
    ata.dialing = ''
    ata.handle_frame(bytes([0xA4, 0x04, 0x31, 0x00]))
    assert ata.dialing == '1'


def test_stalled_schedule_preserves_encoded_interdigit_gap():
    sched = DtmfSchedule()
    assert sched.enqueue(1) and sched.enqueue(1)
    assert sched.poll(0)[3] is True
    # A long media scheduling stall must not let monotonic elapsed time
    # erase the minimum gap in the sample-count RTP timestamp domain.
    for now in (1.0, 1.02, 1.04, 1.06, 1.08, 1.10, 1.12):
        assert sched.poll(now) is not None
    assert sched.poll(1.14) is None
    assert sched.poll(1.16)[3] is True


def test_multichannel_telephone_event_does_not_negotiate():
    body = offer(rtpmap='a=rtpmap:101 telephone-event/8000/2\r\n')
    assert parse_sdp(body) == ('192.0.2.3', 5000)
    assert parse_sdp_dtmf(body) is None


def test_enqueue_racing_call_close_is_rejected(monkeypatch):
    import threading
    ata = LinearATA(dict(CFG))
    call = ata.call = SIPCall(ata, 'test')
    call.established = call.media = True
    call.dtmf_pt = 101
    call.dtmf_events = SUPPORTED_EVENTS
    entered, release = threading.Event(), threading.Event()
    enqueue = call.dtmf_schedule.enqueue
    result = {}

    def parked_enqueue(*args, **kwargs):
        entered.set()
        assert release.wait(1)
        return enqueue(*args, **kwargs)

    def producer():
        try:
            result['value'] = call.send_dtmf('1')
        except Exception as exc:
            result['error'] = repr(exc)

    monkeypatch.setattr(call.dtmf_schedule, 'enqueue', parked_enqueue)
    thread = threading.Thread(target=producer)
    thread.start()
    try:
        assert entered.wait(1)
        call.close()
    finally:
        release.set()
        thread.join(1)
    assert not thread.is_alive()
    assert result == {'value': False}, result
    assert call.dtmf_schedule.pending() == 0


@pytest.mark.parametrize('where', ['fmtp', 'rtpmap', 'mline'])
def test_oversized_event_numbers_disable_events_without_raising(where):
    huge = '9' * 5000
    if where == 'fmtp':
        body = offer(fmtp=f'a=fmtp:101 {huge}\r\n')
    elif where == 'rtpmap':
        body = offer(rtpmap=f'a=rtpmap:{huge} telephone-event/8000\r\n')
    else:
        body = offer(body_pts=f'0 {huge}')
    assert parse_sdp_dtmf(body) is None
    assert parse_sdp(body) == ('192.0.2.3', 5000)
