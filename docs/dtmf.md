# Outbound in-call DTMF — software-tested, IVR validation pending

Status: **software-tested; physical IVR acceptance pending**. Deterministic
scheduler/SDP tests and fake-transport packet tests cover the implemented
contract, including close races and scheduler stalls. These are not proof of
hardware digit notification behavior or PBX digit detection. See
[software reliability evidence](reliability.md) and the
[operator validation checklist](validation.md) for evidence tiers.
A focused real localhost UDP test also checks event payload type 110, shared
sequence/SSRC state and byte-exact synthetic PCMU. It uses no USB or PBX.
Reproduce with:

    LINEATA_LOOPBACK_TEST=1 .re-env/bin/python -m pytest tests/test_dtmf_loopback.py -q

The full offline/localhost suite passed 337 tests before that additional test;
the new UDP DTMF test passed separately. Neither establishes physical digit
detection, long-press timing or IVR interoperability.

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
