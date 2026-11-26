# lineATA roadmap — features and validation

Status: milestones below are planned; the verified baseline records completed evidence.
Baseline commit `8c2de95` (daemon code `d0c2e2a`): single-line real-FreePBX
calls in both directions, dial tone, clear two-way audio, live Linux boot and
USB-loss cleanup, PBX registration renewals, and a live real-shutdown
deregistration probe. Evidence: [docs/native-calls-2026-11-26.md](docs/native-calls-2026-11-26.md),
[docs/reliability.md](docs/reliability.md), [docs/deployment.md](docs/deployment.md),
[docs/validation.md](docs/validation.md).

Authorization: no hardware, service activation, PBX or network action without
explicit task authorization. Preserve packet bytes and media pacing unless
evidence supports a change; no speculative feature pile-up.

## Status legend

- **Verified**: operator-confirmed on real hardware/PBX, or live-checked on the
  target host.
- **Implemented, unverified**: code exists with offline/mock tests only.
- **Missing**: no implementation yet.
- **Optional/deferred**: explicitly not a v1 goal.

## Verified baseline

- Single-line call flow on one KN-3110 + lab FreePBX at `e0f3727` and the
  `d0c2e2a` live checkpoint: boot/init, dial tone, inbound ring/answer, outbound
  INVITE, two-way PCMU speech, both hangup directions, ~50 frames/s pacing,
  `270300` media enable. Live Linux sysfs boot-tty discovery, cold boot,
  296-op init, REGISTER with renewal, USB removal → deregistration reply +
  daemon exit, real-shutdown probe with own-Contact removal acknowledged in the
  final reply.
- Deployment artifacts pass parser-level checks on the target (udev 257.13
  rules rc 0; unit rc 1 only for the absent venv interpreter, rc 0 with
  substitute interpreter — syntax evidence only).

## Priority 1 — stable single-line LAN pilot (implemented, unverified on target)

Use the existing systemd restart approach first; in-process reconnect is
missing but optional until a real need appears — do not block v1 on a
redundant reconnect architecture.

Actions: install the reviewed committed sources to `/opt/lineata` (see
[docs/deployment.md](docs/deployment.md)), run as the non-root `lineata` user
with the scoped udev rules; verify `systemd-analyze verify` on the installed
unit, `systemctl stop` semantics, SIGTERM-with-hardware behavior, automatic
restart after USB loss/replug and after host reboot, PBX/network outage
recovery, and a bounded soak with repeated calls.

Validation tier: real target host + real service + real PBX (operator present).
*Planned* pilot gates (not current achievements): 20 mixed inbound/outbound
calls with no leaked resources, 10 USB-loss/replug recovery cycles, 8 h soak
with stable memory/thread/file-descriptor counts after warm-up. Freeze these
proposed thresholds before execution; document any revision and its reason.
Exit gate: no stuck ringing/calls, no RTP after teardown, no leaked sockets,
manual stop stays stopped, automatic recovery succeeds without manual restart,
and a subsequent call passes the handset regression. Retain exact revision,
structural logs, failure counts and recovery times.

## Priority 2 — UniFi Talk interoperability (missing verification)

Official Ubiquiti guidance:
<https://help.ui.com/hc/en-us/articles/31504711305879-Configuring-Third-Party-SIP-Server-with-UniFi-Access>
documents Talk → Phones → Add Third-Party Device (SIP server/user/password).
That is a plausible route, **not** verified lineATA compatibility; third-party
provider trunk support differs from endpoint registration, and adoption,
management and feature coverage are not guaranteed.

Actions: inspect the installed Talk version/settings for UDP+PCMU support;
first a silent registration/removal cycle without the dongle, then authorized
physical calls/audio/hangup/renewal checks.

Validation tier: real Talk instance first without hardware, then operator-
supervised physical calls.
Exit gate: authenticated registration and renewal, confirmed own-Contact
removal, inbound and outbound ringing/clear two-way speech, both hangup
directions and successive calls on the actual Talk version. Retain sanitized
settings and negotiated transport/codec. A REGISTER 200 alone is insufficient.

## Priority 3 — telephony UX (partially implemented)

- Remote disconnect / busy / reorder / ringback feedback to the analog port.
- Outbound in-call RFC 4733 is implemented and physically IVR-tested on
  FreePBX for 0-9*#, adjacent repeats and a held1 check. Other keys and PBXs remain
  validation items. See [docs/dtmf.md](docs/dtmf.md). Pre-call dialing works;
  standalone in-band detection remains unverified.
- Any tone work must ground frames in existing captures or host-generated audio
  over the proven transport — no invented USB register settings. IVR checks
  cover digits 0-9*#, durations/repeats/end events as negotiated, and must never
  originate extra calls.

Implementation sequence:
1. Define call-progress states and tone start/stop rules for ringing, rejection,
   remote hangup and local ONHOOK. Map supported PBX responses to those states.
2. Add only evidence-backed tone generation/control, cancelling it on answer,
   local hangup, new call and shutdown; preserve the existing steady dial tone.
3. Negotiate telephone-event in SDP and emit correctly timed in-call digit
   events, including end-event repetition and RTP sequencing, only when the
   peer accepts it. Keep pre-call digit collection independent.

Validation tier: offline/mock first, then real PBX, then handset.
Exit gate: busy/unavailable/remote-disconnect feedback is audible and clears
correctly; IVR records exactly the requested 0-9*# sequence, including repeated
digits and long presses; no duplicate digit, accidental call or stuck tone.
Test unsupported event negotiation explicitly and document fallback behavior.

## Priority 4 — hangup "thump" control (open hardware question)

The spike is source-side (final handset→host USB frame, peak 32,124, repeats;
USB↔RTP byte-equal; no post-BYE RTP on that stream). The stationary-receiver/manual-cradle-switch control reproduced it;
next isolate handset/switch/line behavior, optionally using another handset;
distinguish mechanical vs line/firmware before any remedy. No arbitrary
clipping and no fixed ~840 ms lookbehind as a proven-safe fix; if any change is
made, measure speech preservation and latency.

Validation tier: handset checkpoint at a stable milestone (operator-dependent).
Exit gate: record the control's audible result plus matching USB/RTP timing;
either document the identified physical cause or reproduce it independently
before proposing a fix. If changed, compare speech and measured latency against
the unchanged build. A filter that merely hides this sample is not acceptance.

## Priority 5 — evidence-driven broader SIP compatibility (partially missing)

Known documented limits: exact-Contact-URI rewrite falls back to `Expires:`;
only one authenticated INVITE retry (stale/mixed 401/407 limits). Candidate
work: hold/re-INVITE, codec/SDP negotiation, proxy routes; REFER/transfer
optional. Reproduce each target failure before fixing. Current scope is IPv4
UDP + MD5 + PCMU only; TLS/SRTP, modern digests and NAT are scope expansions,
not tested.

Validation tier: offline/mock, then a real target PBX reproducing the failure.
For each selected issue: retain a failing reproduction, implement the smallest
supported correction, preserve bounded authentication retries and teardown,
then rerun calls on that PBX. Hold/resume must restore media in both directions;
proxy/Contact changes must route ACK/BYE correctly. Do not promise a codec or
security mode until its negotiation, media and failure cases are tested.

## Priority 6 — second FXS line / multi-device (optional, deferred)

Needs device evidence plus independent call/state/routing/resource handling —
a dual chip is not proof of two working ports. Analog caller-ID and fax are
explicitly deferred; no fax guarantee.
Exit gate for a future dual-line milestone: identify each physical jack, bind
separate SIP accounts, run simultaneous calls and independent hangups, and
prove no cross-line ringing, digits or audio. Caller-ID needs a separate
phone/region compatibility matrix; fax needs its own transport and success
criteria. Neither follows from working voice.

## Work queues

Away-from-home (no hardware/operator needed):
- offline/mock regressions for Priorities 3 and 5;
- deployment install rehearsal on a scratch Linux VM (parser + non-root +
  udev only, no service activation);
- Talk version/settings inspection and silent registration/removal once the
  target and credentials are available and the test is authorized; no calls;
- PBX outage/recovery tests using the silent probe, with retained timing and
  no wildcard deregistration.

Operator-dependent queue:
- Priority 1 pilot on the real target (service, signals, restarts, soak);
- Priority 2 physical calls against Talk;
- Priority 4 thump checkpoint with stationary receiver;
- analog tone/DTMF verification and simultaneous dual-line tests if selected.

All live access still requires task authorization; silent PBX checks do not
require an operator at the handset. Use [docs/validation.md](docs/validation.md)
for capture hygiene and stop conditions. At every stable implementation
milestone, run the relevant offline checks, then a short authorized handset
regression before layering further hardware-dependent changes.
