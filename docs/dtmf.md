# Outbound in-call DTMF — FreePBX IVR baseline verified

Status: **real FreePBX IVR baseline verified for 0-9*#**. The operator heard
the complete sequences read back, and the PBX logged exactly 0123456789*# and
11223344**## with status OK using endpoint mode rfc4733. A captured call also showed four
physical notifications becoming four correctly negotiated events, with 939
unchanged USB/RTP voice payloads and no sequence gaps.

Deterministic/fake-transport checks and real localhost UDP tests cover the
scheduler, negotiation and packet boundaries. A two-second hold of the first
1 in the repeated sequence was recognized once, with exact readback and PBX
status OK. RTP event duration is still fixed nominal100ms, not the physical
hold duration. Other held keys, A-D and other PBXs remain separate checks. See the
[call evidence](native-calls-2026-11-26.md) and
[operator checklist](validation.md).

    LINEATA_LOOPBACK_TEST=1 .re-env/bin/python -m pytest tests/test_dtmf_loopback.py -q

## Scope

Outbound-only, negotiated [RFC 4733](https://www.rfc-editor.org/rfc/rfc4733)
telephone-event sending during an established call, driven by the existing
in-call USB digit (`0xA4`) notifications. This milestone does **not** render
or act on received named events. Pre-call digit collection (dialing) is
unchanged and independent.

## Modes

- `--dtmf-mode rfc4733` (default): the outgoing offer advertises a dynamic
  payload type (101) for `telephone-event/8000` with events `0-15` alongside
  PT 0 PCMU. When the peer negotiates it, in-call digits are sent as RFC 4733
  event packets.
- `--dtmf-mode inband`: advertises no event payload type and keeps the previous
  behavior (digits only go to the PBX if it renders inband audio itself). This
  is an explicit fallback, not a proven path.
- A PCMU-only peer uses the existing voice path: the call proceeds on PCMU and DTMF
  events are simply not sent.

## Negotiation rules (summary)

- The event payload type is selected only from the **same** first supported
  PCMU audio media section (dynamic range 96-127); contradictory mappings,
  wrong clock rate, malformed parameters or a disjoint event set disable DTMF
  but **must not reject** an otherwise valid PCMU call. The answer echoes the
  offered event PT and only the intersected event set. Offers/answers that
  arrive during authentication retries carry the same capabilities.

## Sending behavior

- Digits `0-9`, `*`, `#`, `A-D` are accepted when the call is established,
  media is active and the event was negotiated and is supported by the peer.
- Each USB digit notification maps to one fixed nominal **100 ms** event
  (800 samples at 8000 Hz). Hardware press/release timing and held-key
  duration are **unverified** — no long-press preservation is claimed.
- Events are queued in a bounded queue (32); overflow, invalid digits or an
  unnegotiated call are ignored without disturbing the USB thread. Pending or
  non-established calls ignore in-call digits.
- One RTP media thread owns all sends: event packets share the PCMU stream's
  SSRC and sequence counter, carry a constant timestamp per event, set the
  marker bit only on the first packet, ramp duration at 8000 Hz and repeat the
  end event; minimum 70 ms interdigit gap on the 20 ms grid; no burst catch-up
  after a scheduler stall.
- Queue/active state is discarded on close/hangup/shutdown; no event is emitted
  after the media worker stops.

## Audio preservation and the duplication caveat

PCMU bytes and 20 ms pacing are preserved; named-event packets are interleaved
in the same RTP stream and no DSP suppression or voice filtering is applied.
Because inband audio may also carry the digit, **inband + out-of-band
duplication at the PBX is unverified** until a physical IVR/handset check
(use `--dtmf-mode inband` if duplication is observed).

Capture-analysis warning: when a telephone-event payload shares the RTP
sequence numbers, PT-0-only sequence discontinuities reported by
`tools/audit_media.py` are **not automatically packet loss** — account for the
interleaved event packets before concluding loss.

## Later acceptance (real handset / IVR, only when authorized)

Physical validation must cover, at minimum: digits `0-9*#` (and `A-D` when
negotiated), repeated digits, exactly one detection per digit (no double
detection from inband+OOB duplication), the long-press limitation above, and
no audio regressions versus the hardware-verified baseline in
[`native-calls-2026-11-26.md`](native-calls-2026-11-26.md). IVR checks must
never originate extra calls. See [../ROADMAP.md](../ROADMAP.md) for the
planned validation tiers and [../README.md](../README.md) for scope.

## Lab recognition fixture

The operator-authorized internal test number is *8899. Dial *8899# from the
analog handset, wait for the beep and enter twelve digits. The isolated custom
dialplan uses Read with no terminator, so * and # are collected literally. It
logs the exact string, reads it back and hangs up, with a60-second absolute
limit. The lab endpoint uses rfc4733, so this checks named-event recognition.

The fixture is in extensions_lineata_test.conf, included from the previously
empty extensions_custom.conf; the original was backed up before editing.
Normal extension and external routing were not changed. Remove only that
include and dedicated file when retiring the test, checking for later edits
before restoring anything, then reload the dialplan.
