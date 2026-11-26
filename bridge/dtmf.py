"""RFC 4733 (telephone-event) outbound DTMF support — pure state, no I/O.

The dongle reports each keypress as a single USB 0xA4 ASCII event with no
verified press/release duration, so a fixed nominal event duration is used
(no long-press preservation claim). This module holds only deterministic
packet/schedule state: the single RTP media thread in bridge/ata.py owns all
packet sends and polls DtmfSchedule on its 20 ms ticks, interleaving named
event packets with unchanged PCMU in the same RTP stream (same SSRC and
sequence counter, per RFC 4733 sec 2.5.1.2).

Standards: RFC 4733 https://www.rfc-editor.org/rfc/rfc4733.txt
(sec 2.3 payload, 2.4/2.4.1 fmtp, 2.5.1.2 transmission, 2.5.1.4 final
retransmission, 3.2 Table 3 DTMF codes).
"""
from __future__ import annotations

import threading

# RFC 4733 sec 3.2 Table 3: DTMF named events.
DTMF_EVENTS = {c: i for i, c in enumerate("0123456789*#ABCD")}
SUPPORTED_EVENTS = frozenset(range(16))  # we support DTMF 0-15 and nothing else
DEFAULT_PT = 101                          # dynamic PT offered for telephone-event

TICK_SECONDS = 0.020   # one RTP packetization interval (matches a=ptime:20)
TICK_UNITS = 160       # 20 ms in 8000 Hz timestamp units
EVENT_SECONDS = 0.100  # fixed nominal duration per hardware digit event
EVENT_UNITS = 800      # 100 ms in 8000 Hz timestamp units
MIN_GAP_SECONDS = 0.070  # interdigit gap: nominal event end -> next event start
VOLUME = 10            # -10 dBm0 (sec 2.3.4); constant, tone events
END_REPEATS = 3        # final packet sent 3 times total (sec 2.5.1.4)
MAX_QUEUE = 32         # bounded pending-event queue

# Per-event packet plan at 20 ms ticks: cumulative update durations with E=0,
# then the E=1 final at the full duration repeated END_REPEATS times (8 total).
_UPDATE_DURATIONS = (160, 320, 480, 640, EVENT_UNITS)
SCHEDULE = [(d, False) for d in _UPDATE_DURATIONS] + [(EVENT_UNITS, True)] * END_REPEATS


def digit_to_event(digit):
    """Map one DTMF character to its RFC 4733 event code, or None if invalid."""
    if not isinstance(digit, str) or len(digit) != 1:
        return None
    return DTMF_EVENTS.get(digit)


def build_event_packet(event, end, duration, volume=VOLUME):
    """4-byte named-event payload (sec 2.3 Fig. 1): event | E|R|volume | duration.

    R bit is always 0 (sender MUST, sec 2.3.3); duration is big-endian 16-bit.
    """
    if not 0 <= event <= 255:
        raise ValueError(f"event out of range: {event!r}")
    if not 0 <= duration <= 0xFFFF:
        raise ValueError(f"duration out of range: {duration!r}")
    b1 = (0x80 if end else 0x00) | (volume & 0x3F)
    return bytes((event, b1)) + duration.to_bytes(2, "big")


def parse_events_list(value):
    """Parse an a=fmtp telephone-event list ('0-15,66,70'); None if malformed.

    No whitespace allowed (sec 2.4); each element is a code or code range.
    """
    if not value or any(c.isspace() for c in value):
        return None
    events = set()
    for tok in value.split(","):
        if "-" in tok:
            lo, _, hi = tok.partition("-")
            if not (lo.isascii() and hi.isascii() and lo.isdigit()
                    and hi.isdigit() and len(lo) <= 3 and len(hi) <= 3):
                return None
            a, b = int(lo), int(hi)
            if not 0 <= a <= b <= 255:
                return None
            events.update(range(a, b + 1))
        elif tok.isascii() and tok.isdigit() and len(tok) <= 3:
            n = int(tok)
            if n > 255:
                return None
            events.add(n)
        else:
            return None
    return events


def format_events(events):
    """Format an event set as compact ranges, e.g. '0-15' (sec 2.4.1)."""
    ev = sorted(events)
    parts = []
    i = 0
    while i < len(ev):
        j = i
        while j + 1 < len(ev) and ev[j + 1] == ev[j] + 1:
            j += 1
        parts.append(str(ev[i]) if j == i else f"{ev[i]}-{ev[j]}")
        i = j + 1
    return ",".join(parts)


class DtmfSchedule:
    """Bounded pending queue + per-event packet schedule, driven by caller time.

    Thread-safe and I/O-free: the RTP media thread calls poll(now) once per
    20 ms tick. poll() returns at most one event update per call and never
    burst-catches-up after a stall (a missed due time reschedules from now).
    Consecutive identical digits get distinct event start times because the
    interdigit gap keeps the next start on a later tick.
    """

    def __init__(self, tick=TICK_SECONDS, duration=EVENT_SECONDS,
                 gap=MIN_GAP_SECONDS, max_queue=MAX_QUEUE):
        self.tick = tick
        self.duration = duration
        self.gap = gap
        self.max_queue = max_queue
        self._lock = threading.Lock()
        self._queue = []       # event codes awaiting their start tick
        self._active = None    # (event, start, next_due, index)
        self._last_end = None  # nominal end (start + duration) of the last event
        self._closed = False

    def enqueue(self, event, now=None):
        """Queue one event code; False when the bounded queue is full."""
        with self._lock:
            if self._closed or len(self._queue) >= self.max_queue:
                return False
            self._queue.append(event)
            return True

    def clear(self):
        """Discard queued and active events (close/hangup/shutdown path)."""
        with self._lock:
            self._queue.clear()
            self._active = None
            self._last_end = None

    def pending(self):
        with self._lock:
            return len(self._queue) + (1 if self._active is not None else 0)

    def close(self):
        """Permanently reject racing producers and discard pending state."""
        with self._lock:
            self._closed = True
            self._queue.clear()
            self._active = None
            self._last_end = None

    def poll(self, now):
        """Return (event, end, duration_units, first) for this tick, or None.

        At most one update per call. first=True marks the event's initial
        packet (RTP marker bit set by the caller); the timestamp is constant
        per event and supplied by the RTP thread.
        """
        with self._lock:
            if self._active is not None:
                event, start, due, index = self._active
                if now < due - 1e-9:
                    return None
                duration, end = SCHEDULE[index]
                first = index == 0
                due += self.tick
                if due <= now:  # scheduler stall: skip missed slots, no burst
                    due = now + self.tick
                if index + 1 < len(SCHEDULE):
                    self._active = (event, start, due, index + 1)
                else:
                    self._active = None
                    # After a stall, RTP's sample-count clock has advanced only
                    # one step per actual tick. Reserve the remaining gap after
                    # the final repetitions too, rather than treating the
                    # elapsed wall time as an already-encoded interdigit gap.
                    self._last_end = max(start + self.duration,
                                         now - (END_REPEATS - 1) * self.tick)
                return event, end, duration, first
            if not self._queue:
                return None
            if self._last_end is not None and now < self._last_end + self.gap - 1e-9:
                return None  # interdigit gap not yet elapsed
            event = self._queue.pop(0)
            self._active = (event, now, now + self.tick, 1)
            duration, end = SCHEDULE[0]
            return event, end, duration, True
