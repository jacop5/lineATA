# Independent takeover and silent follow-up

Date: 2026-11-25, Europe/Rome. The operator confirmed the native phone rang
and that they picked it up, then paused testing. Subsequent work was authorized
with **no ringing and no user actions requested**. That restriction remains active. The follow-up
below used software tests, registration-only SIP and heartbeat-only hardware.

## Evidence obtained

Read HANDOFF.md, the uncommitted AUDIT.md, protocol notes, current and legacy
bridge code, tests, experiment scripts, git history, shim logs and raw pcaps.
AUDIT.md itself is a previous, unverified audit, not this session's conclusion.
Existing uncommitted files were retained during the audit. Local deployment
snapshots and raw captures remain outside version control.

Connected to root@10.9.12.200 (password supplied by operator, NOT saved).
Initial dongle was PID 0002; no nvox/native daemon was running. Preserved old
remote ata.py/native.py under artifacts/remote-baseline/. Additional old raw
captures usb.call2, usb.cold, usb.inc are in captures/2026-11-25/reaudit/.
Original deployment /tmp/linbridge was not replaced. New experiment files
were staged separately in /tmp/lineata-audit-20261125/.

1. Native baseline: boot via `b`, 296 captured init operations, matching
   responses, no repeat-until-zero interpretation of register 0x1d, followed
   by 15 seconds of alternating 1 Hz heartbeat. Exit 0, all heartbeats ACKed.
2. Prepared ring: reset to bootloader, boot and init as above, 15 seconds of
   baseline; send both CALL_ANNOUNCE frames; wait 2 seconds; read and ring ON
   each command family; wait ~400 ms; read and ring OFF each family; observe
   10 further seconds. All control writes wait for matching response, using
   one reader throughout. Exit 0. The user confirmed the phone rang and
   they picked it up. OFFHOOK was captured at experiment-relative 21.595694 s.
   Reads before ON were `600344`/`400344`; before OFF `600311`/`400311`.
   Files: prepared-ring.log/.pcap and baseline.log/.pcap under reaudit/.
   Experiment source: artifacts/experiments/audit_usb.py; exact deployed ATA
   code: artifacts/remote-baseline/ata-ring-experiment.py.

This proves a native prepared ring pulse works on this dongle. It does NOT
isolate preparation as the old crash's cause: the experiment also corrected
heartbeat, command/reply serialization and initialization methodology.
No SIP or media tests were performed in this session. No daemon remains
running from these bounded experiments; SSH was closed on pause.

## Audit corrections and remaining hypotheses

- `poll10.py` omits heartbeat, has two concurrent readers and locks only
  writes. It cannot establish that 10 ms polling causes firmware crashes.
  ringtest2.py has the same confounds. Read errors are often swallowed.
- `Errno 19` proves loss of device access, not firmware crash by itself.
- 296 init frames match the post-country-reenumeration warm stream; this
  does not prove a complete country-independent cold startup. Old captures
  show c602/c00300 on the previous USB address before reenumeration.
- Both command families ringing is demonstrably supported. Physical port
  numbering is still not established by a one-port experiment.
- Nonzero startup read replies refute the assumption that EVERY 0x1d read
  must be repeated until zero. Do not restore that loop.
- Total frame length semantics and 160-byte voice payloads are structurally
  supported. PCMU is strongly suggested, but ff silence and a reported oracle
  call alone are not a byte-for-byte USB/RTP codec equivalence test.
- Use one timestamp origin. In raw warm pcap, relative to FIRST record:
  OFFHOOK 5.832131, digits 8.211436/9.415170/11.020177, ONHOOK 13.942162.
  Inbound raw pcap: preparation 4.423165/4.426440, first ring
  6.423465/6.426998, OFFHOOK 7.168796, answer 7.230507..7.234664,
  ONHOOK 16.203852. The earlier audit mixed device-relative timestamps.
- Inbound oracle capture has 445 OUT voice packets (7.340305..16.212250)
  and 397 IN voice packets (7.444051..15.364014). IN audio stops ~0.84 s
  before ONHOOK. Do not attribute that to native frame loss without evidence.
- All bulk OUT submissions in the four original pcaps had successful USB
  completions. Completion/ACK is not proof of intended electrical behavior.

## Local changes and validation

- tools/usbmon_parse.py now rejects wrong link types, truncated record
  headers/data, snaplen truncation and truncated USB payloads instead of
  silently accepting incomplete evidence. All four original pcaps parse.
- Added tests/test_native_transport.py: fragmented/coalesced audio + hook,
  voice payload containing event-like bytes, malformed lengths, disconnect
  propagation, in-call hook/digit handling, RTP header variants, SIP CANCEL.
- Reproduced a SIP bug before fixing it: 200 to CANCEL was interpreted as
  200 to INVITE, marking the dialog established and generating a bogus BYE.
  bridge/ata.py now checks response CSeq method and number in the INVITE
  worker. Full suite: 21 passed. This fix is local, not deployed/tested at PBX.
- The original 13 passing tests mostly cover legacy paths; some merely test
  speculative voice headers or inspect source text. Passing them does not
  validate the current daemon or the protocol guesses.

## Resume priorities

1. Preserve the current no-ringing/no-user-action restriction. No credentials
   are stored here. No test process or SSH connection remains active.
2. Test native full SIP inbound call when user participation is available:
   ring, answer, byte
   counters, two-way speech, local ONHOOK -> BYE, remote BYE, repeat calls.
   Use remote /tmp/nvox.conf credentials in memory only; do not copy config
   or expose passwords in command lines/logs. Outbound and CANCEL races next.
3. Functional generic PBX daemon is NOT yet complete or certified stable.

## Silent follow-up results

### Reproduced defects and implementation changes

- `bridge/transport.py` now serializes runtime controls as well as the
  successful ring experiment did: one outstanding command, matching opcode
  response required, bounded queue, explicit timeout without automatic replay.
  Both owner and other threads enqueue. Audio uses its separate paced path.
  An unsolicited event cannot release a different command. Pending ring edges
  have guards so cancellation/answer discards them before transmission.
- Retained outbound INVITE transactions separately from the active call.
  Clearing the active call on ONHOOK no longer prevents a late final response
  reaching the waiting transaction. A simulated CANCEL/200 race produces ACK
  then BYE; 200 to CANCEL cannot establish the INVITE dialog.
- Cache final responses to prevent retransmitted cancelled INVITEs from
  starting a new ring, and to answer repeated BYEs without repeated USB
  teardown. Cache outbound ACKs for repeated final INVITE responses. Incoming
  200 responses retransmit until ACK or timeout. These are software-tested.
- Normalize case-insensitive/compact SIP headers and preserve multiple Vias
  in responses. Correlate registration responses by Call-ID and CSeq. Ignore
  malformed status/request lines; match CANCEL to the original transaction.
- SDP now selects audio's media-level connection rather than the first
  connection in the message, and rejects unusable addresses, disabled/invalid
  ports, unsupported transport and offers without PCMU before USB preparation.
- Reserve the actual RTP socket before advertising its port. Close it on
  failed/cancelled calls. Old call cleanup cannot erase a newer call's audio.
- Digest parsing handles quoted qop lists. Unsupported algorithms/auth-int
  fail explicitly rather than emitting an incorrectly labeled MD5 result.
- `LINEATA_SIP_PASSWORD` is supported so the SIP secret need not be in process
  arguments. Runtime/dev dependencies are recorded. No real secrets saved.

### Verification

- 59 tests pass; two real-socket loopback/integration tests are opt-in and
  skipped by default. Both passed when enabled with local UDP permitted: 160 bytes
  reached the peer unchanged; a reverse RTP packet with CSRC, extension and
  padding reached the USB-bound buffer unchanged; the media thread stopped.
  The suite includes 13 historical tests, now explicitly labeled as such.
- The combined local integration test runs the actual SIP listener, USB
  command queue/event loop and RTP worker against UDP loopback peers and a
  synthetic USB device. Two successive incoming calls complete; the first
  exchanges exact audio bytes in both directions and ends on simulated local
  ONHOOK, the second ends by peer BYE. ACK processing, dialog headers, call
  cleanup and media shutdown are checked. This invokes no real USB or PBX.
- `tools/sip_register_probe.py` registered extension 2005 at 10.9.12.136 using
  the current daemon SIP code, then removed only its own Contact. Both checks
  passed, including a repeat after the listener/digest changes. No calls,
  USB access or ringing. Password was entered through a hidden prompt.
- `artifacts/experiments/silent_queue_probe.py` initialized the actual dongle
  and ran the current `_usb_loop` for 20 seconds, with every runtime write
  except `a00300`/`a00301` blocked and handset events ignored. All 20 heartbeat
  responses arrived. Exit 0. No SIP, tones or ring commands were enabled.
  Evidence: `captures/2026-11-25/reaudit/silent-queue.{log,pcap}`; exact ATA
  snapshot: `artifacts/remote-baseline/ata-silent-experiment.py`; deployed
  module hashes: `artifacts/experiments/silent-queue-source.sha256`.
- The revised `tools/audit_captures.py` excludes CDC console data based on USB
  descriptors, groups by bus/address, pairs OUT submissions with completions,
  and parses fragmented/coalesced frames. Regression tests cover wrong link
  type, partial capture records/USB headers/data, boot-vs-service classification,
  coalesced events and short OUT completion. Reproducible output and hashes:
  `docs/reaudit-captures-2026-11-25.json`. Earlier audit JSON is preserved.

The updated scheduler's **ring/answer/hangup batches together with SIP and real
handset audio remain untested on hardware**. The confirmed ring pulse used
the isolated experiment's synchronous transactions. Silence-only hardware and
loopback tests do not establish complete ATA operation.
