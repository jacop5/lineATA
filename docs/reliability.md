# Software reliability evidence and offline test guide

This document preserves reviewed **software** evidence in version control. Hardware
claims belong only to the operator-confirmed captures documented in
[`native-calls-2026-11-26.md`](native-calls-2026-11-26.md) at commit
`e0f3727` (basic native single-line flow: boot/init, dial tone, inbound/outbound
calls, two-way PCMU speech, hangup both sides, successive calls, `270300` media
enable, 20 ms pacing) and to the live `d0c2e2a` Proxmox checkpoint (real Linux
sysfs/boot, one operator-confirmed inbound call; see
[`validation.md`](validation.md) evidence matrix). Software tests are **never
hardware proof**; the captured wire tables and media pacing are unchanged by
the software rework.

## Running the offline suite

```sh
.re-env/bin/python -m pytest -q                      # full offline suite
.re-env/bin/python -m pytest tests/test_registration.py tests/test_registration_adversarial.py -q
.re-env/bin/python -m pytest tests/test_usb_lifecycle.py tests/test_usb_identity.py tests/test_boot_tty_selection.py tests/test_deployment_config.py -q
.re-env/bin/python -m pytest tests/test_shutdown.py tests/test_shutdown_adversarial.py tests/test_shutdown_edges.py tests/test_shutdown_process.py tests/test_shutdown_incoming.py -q
.re-env/bin/python -m pytest tests/test_cli_validation.py tests/test_startup_cancellation.py -q
LINEATA_LOOPBACK_TEST=1 .re-env/bin/python -m pytest tests/test_rtp_loopback.py -q   # opt-in real UDP loopback
```

The shutdown tests cover the graceful-shutdown paths with mocked USB/SIP plus
real OS-signal process tests that fake USB/SIP startup (no real service, no
device, no network).

Run the commands against the revision being evaluated; passing results apply
only to the code and fault scenarios exercised. The optional UDP test uses
localhost sockets and simulated USB.

## Accepted registration corrections (software, reviewed)

REGISTER handling was reworked and lead-reviewed; the covered behaviors are:
own-Contact lease parsing (every `Contact:` header, comma-split respecting
`<...>`, case-folded whole-URI match, entry's own `expires=`, explicit `expires=0` kept as
0 and never reported positive, missing → `Expires:` fallback), bounded
backoff before exponentiation, 423 Min-Expires handling, retransmission
correlation (identical bytes at 0.5/1/2 s plus a bounded final response window,
never sending after stop), 401→`WWW-Authenticate` / 407→`Proxy-Authenticate`
status-specific challenges, provisional responses with a cached nonce never
yielding a fresh 'challenge', settled transactions immune to delayed/duplicate
responses, new request resetting per-transaction state. Counterexample tests in
`tests/test_registration.py` and `tests/test_registration_adversarial.py`
reproduce each defect before the fix and pass after.

**Known limitation:** Contact URIs with extra URI parameters that a PBX
rewrites do not match exactly; the code then falls back to the `Expires:`
header (documented compatibility limit, no SIP URI parser added).

## Outgoing authentication scope

`tests/test_invite_auth.py` exercises the real INVITE builders with fake UDP
responses. A single 401 produces an authenticated retry with a new branch and
incremented CSeq; a 407 uses `Proxy-Authorization`. Matching final responses
are ACKed, and an unrelated CSeq challenge is ignored. Persistent challenges
stop after one authenticated retry.

This also limits interoperability: a second challenge, including a fresh
`stale=true` nonce or a 401 following a 407, ends the attempt instead of
performing another authentication leg. These are observed compatibility
limits, not evidence that the physically verified direct-PBX calls fail.

## CLI argument validation (software)

`main()` validates arguments before the ATA is constructed, using the real
argparse path with a recording sentinel (`tests/test_cli_validation.py` —
`--help` short-circuits parsing and proves nothing about values): `--pbx-port`
must be 1-65535, `--sip-port` 0-65535 (`0` keeps an ephemeral local port), the
dial timeout must be finite and positive, and `--local-ip` must be an IPv4
literal — wildcard, multicast, limited-broadcast, hostnames and IPv6 are
rejected. The parser never queries DNS or network interfaces and does not
infer address assignment or routing class (e.g. `240.0.0.1` parses). `--pbx`
still accepts hostnames; the secret flag/environment priority is unchanged.

## Startup cancellation (software evidence only)

Startup helpers accept an optional `should_cancel=None`; connect/init/run pass
a combined stop/request check. Discovery, tty and init checkpoints stop
cooperatively, retry/startup waits poll in 50 ms slices, owned resources are
disposed, and no new reset, retry or USB thread is started after a checkpoint observes cancellation.
The positive control preserves the `b` boot byte and the 296-operation init.
Evidence: `tests/test_startup_cancellation.py` provides software evidence only; fake-clock measurements are not physical
timing. Limits: an in-flight native call cannot be preempted; the 10 s shutdown
coordinator budget does not guarantee a total startup bound; an interruption
can leave the firmware partially initialized, so an actual restart or cold
boot after an interruption requires authorized hardware validation.

## RTP socket ownership (software evidence)

Media preparation binds a local candidate outside the resource lock, then
commits ownership under the same lock used by call closure. A candidate that
finishes after closure is closed without publication; concurrent preparations
keep one winner and close the unpublished loser. Incoming answer handling
rechecks call/shutdown state after preparation. No resource lock is held over
socket creation, binding, closing or network I/O.

`tests/test_rtp_lifecycle.py` exercises the real preparation method with fake
socket bind barriers. The optional `tests/test_rtp_loopback.py` additionally
checks real localhost SIP/RTP sockets with synthetic USB and audio. Neither
proves physical handset behavior or interoperability with the real PBX.

## USB lifecycle status (do not overclaim)

- USB failure handling passed source review, deterministic regressions and
  independent fault-injection probes: claim failures, tty open/config/write
  errors, initialization disposal, cleanup on fault and normal stop, queue
  overflow stopping the daemon, serialized submit/cleanup and explicit call
  resource closure. These checks cover software failure paths only.
- The boot-tty identity helper (`bridge/usb_identity.py`,
  `tests/test_usb_identity.py`, `tests/test_boot_tty_selection.py`) passes
  synthetic fixture tests **and** was exercised on real Linux sysfs at
  `d0c2e2a` (returned `/dev/ttyACM0`; cold boot then enumerated as
  `31b7:0001` with all 296 init operations acknowledged).
- **There is no in-process USB reconnect.** If the transport faults, the
  daemon's fatal handler stops the process (exit status 2); recovery today
  means process restart. A live USB removal at `d0c2e2a` was followed by a
  confirmed PBX reply to `REGISTER expires=0` and daemon exit (acknowledgment,
  not independent contact-list verification); automatic restart via the draft
  systemd unit on the target remains **unvalidated**.
- **Graceful shutdown is implemented and offline-tested** (mock-only plus
  real OS-signal process tests with faked USB/SIP startup): SIGTERM/SIGINT
  only set a shutdown-request event in the handler; `shutdown()` (default
  budget 10 s, idempotent, deadline-bounded) queues captured state-appropriate
  USB teardown frames, drains the USB queue for at most 1 s, stops and joins
  the registrar before sending a Contact-specific `REGISTER expires=0`
  (never wildcard), keeps the SIP listener alive for the removal reply, gives
  pending outgoing INVITEs a bounded cancellation grace (at most 1 s within
  the overall budget), and closes resources even on a missing reply or partial
  startup. Requested shutdown exits 0; fatal USB stop exits 2. An unanswered
  incoming INVITE is answered with a correlated final 480 (same To-tag as the
  180) and no BYE is sent before establishment; admission state commits under
  lock and delayed announce guards discard after a request
  (`tests/test_shutdown_incoming.py`). Regression
  tests: `tests/test_shutdown.py`, `tests/test_shutdown_adversarial.py`,
  `tests/test_shutdown_edges.py`, `tests/test_shutdown_process.py`,
  `tests/test_shutdown_incoming.py`. Real-PBX
  Contact removal and BYE receipt are **not** validated by these tests.

Incoming final-response choices are serialized per call: an answer, shutdown
rejection, CANCEL rejection or media-allocation error cannot send contradictory
200/480/487/500 finals. Teardown waits for a chosen answer's send before BYE.
USB writes and cleanup stay outside this serialization; media allocation
precedes it. `tests/test_answer_finalization.py` and
`tests/test_answer_concurrency.py` use fake transport barriers to check both
winning orders, including an answer already in flight. These remain software
regressions, not physical timing or full SIP conformance evidence.

## Deployment draft

[`deployment.md`](deployment.md) is an **offline draft** (systemd unit, env
example, udev rules). Parser-level checks ran on the real target (udev 257.13):
original udev rules `udevadm verify` rc 0; original unit `systemd-analyze
verify` rc 1 solely for the absent `/opt/lineata/.venv/bin/python`
prerequisite, rc 0 with only the interpreter substituted — syntax evidence
only, not installed-service acceptance. No service was installed or
activated, and runtime behavior (restart, stop) remains unvalidated on Linux.
Install from a reviewed commit as described in its instructions.

## Methodological corrections to earlier test claims

- `--help` short-circuits argparse, so passing values together with `--help`
  proves nothing about value parsing; integer acceptance is proven by
  parser-boundary probes with a guaranteed later error: `--pbx-port 5060
  --login` reaches the missing-login-value error, while `--pbx-port not-a-port
  --login` fails integer conversion first. Both exit before device or network
  access.
- Mocked retry tests must attach distinct fake devices and assert the exact
  disposal list; a fake returning success without reattaching made
  `dispose(None)` inflate disposal counts.
- Probabilistic thread-hammer overflow tests were replaced by deterministic
  producer/cleanup event-ordering tests.
- A fixture that processes a response only after `shutdown()` returned passed
  only because the pending call's resources were still open (orphaned);
  post-shutdown race fixtures must start a real bounded worker before shutdown
  and synchronize on wire events while the socket is still open.
- A Python signal can interrupt `threading.Event.wait()` while its condition
  is held; calling `Event.set()` from that handler can deadlock on the same
  lock. The signal request instead sets a plain flag; ordinary waiters poll it
  using a separate sleeper.
- Real OS signals (SIGTERM/SIGINT) are tested by delivering actual signals to a
  child running the real `main()` with faked USB/SIP startup — not by calling a
  proposed handler directly.

- Cancellation tests must set the daemon's actual request flag, not only a
  trace marker. Count subsequent operations separately from the operation
  that injected the request, and disable unrelated background actors.
- Test the implementation under review: park a fake socket's `bind()` while
  running the real media-preparation method. Replacing that whole method with
  a fake does not establish whether its ownership checks work.

## Hangup transient ("thump") — current evidence

Structural analysis of the `d0c2e2a` capture (partial USB completions
retained; mirrored RTP deduplicated): the spike is already present in the
**final handset→host USB frame** (peak 32,124; RMS 9,195 vs ≤100 for the 14
preceding quiet frames), and USB↔RTP payloads match byte-for-byte 352/352, so
the daemon forwards it unchanged — the transient is source-side
(handset/dongle), not a teardown artifact; the signature repeats across runs
(same final-frame position, same peak 32,124, ~0.84 s before ONHOOK/BYE), and
no RTP followed BYE on the host→PBX stream in that capture. No suppression is
implemented (a zero-delay energy filter could clip speech; a lookbehind needs
end detection). Remaining control: stationary receiver with manual
cradle-switch hangup. Still open: the line/firmware cause.

## Still unvalidated

Broader PBX compatibility, the second FXS port, long-duration soak, automatic
service restart on the target host, and an independent PBX database/registrar
query after deregistration (live probe observed own Contact absent from the
final reply, but no separate query was made). Single-line call and both hangup
directions were operator-confirmed at `d0c2e2a` (see
[`native-calls-2026-11-26.md`](native-calls-2026-11-26.md)). Any live behavior
of newer software changes beyond that checkpoint remains mock/synthetic only.
