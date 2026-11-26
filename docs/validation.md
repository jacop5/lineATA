# Future operator validation checklist

Status: this is a **version-controlled checklist, not proof of execution**. No
live experiment was run for it. Steps that ring phones or place calls are
future steps only: they require an operator physically present and explicit
authorization at the time. All addresses, logins and secrets are placeholders;
never record real credentials. No broad USB access is ever required — the
udev rules stay scoped to vendor `31b7` products `0001`/`0002`.

## 1. Evidence matrix (as of this writing)

| Area | Status | Evidence |
| --- | --- | --- |
| Basic single-line call flow (dial tone, inbound ring/answer, outgoing INVITE, two-way PCMU speech, hangup both sides, successive calls) | Operator-confirmed on hardware at commit `e0f3727` | `docs/native-calls-2026-11-26.md` + local captures |
| Live Linux checkpoint at `d0c2e2a` (Proxmox, real dongle): sysfs boot-tty `/dev/ttyACM0`, cold boot `31b7:0002→0001`, 296/296 init ACKs, REGISTER 299 s lease, operator-confirmed inbound AND outbound calls (~50 frames/s) — both hangup directions passed (remote BYE left the analog handset silent; ONHOOK disconnect on inbound) | Operator-confirmed | `docs/native-calls-2026-11-26.md` |
| Live USB removal at `d0c2e2a` (daemon exit after PBX-confirmed `expires=0` reply) plus live real-registrar + real-`shutdown()` probe (rc 0): 299 s lease, `deregister:ok`, own Contact absent from the final REGISTER reply, threads stopped, socket closed, no USB/calls | Reported live; reply-list absence observed, independent registrar/PBX-database query NOT made | `docs/native-calls-2026-11-26.md` |
| Closing "thump": spike present in the final handset→host USB frame (peak 32,124, repeats), USB↔RTP byte-equal 352/352, no post-BYE RTP on that stream → source-side, forwarded unchanged; no suppression implemented | Structural capture analysis at `d0c2e2a` | `docs/native-calls-2026-11-26.md`; remaining control: stationary receiver / manual cradle-switch hangup |
| Wire frame tables, `270300` media enable, 20 ms media pacing (~50 frames/s) | Hardware-verified at `e0f3727`; unchanged since | `PROTOCOL.md`, captures, `tests/test_media_pacing.py` |
| Registration corrections (own-Contact lease, bounded backoff, 423, retransmit correlation, 401/407 status-specific challenges) | Mock-only offline tests; reviewed | `tests/test_registration.py`, `tests/test_registration_adversarial.py`, `docs/reliability.md` |
| USB lifecycle hardening and boot-tty identity helper | Reviewed software evidence at commit `cf6a367`; synthetic fixture tests only — **no real hardware proof** | `tests/test_usb_lifecycle.py`, `tests/test_usb_identity.py`, `tests/test_boot_tty_selection.py` |
| Graceful shutdown (SIGTERM/SIGINT request, bounded idempotent teardown, own-Contact `expires=0` removal, pending-call cancellation grace, exit codes 0/2) | Mock-only offline tests plus real OS-signal process tests with faked USB/SIP startup — **not live acceptance** | `tests/test_shutdown.py`, `tests/test_shutdown_adversarial.py`, `tests/test_shutdown_edges.py`, `tests/test_shutdown_process.py` |
| Incoming unanswered INVITE on shutdown: correlated final 480 (same 180 To-tag), no BYE before establishment | Mock-only offline tests — no real PBX receipt or hardware proof | `tests/test_shutdown_incoming.py` |
| CLI argument validation before ATA construction (port ranges, ephemeral `--sip-port 0`, finite dial timeout, IPv4-literal `--local-ip`; no DNS/interface queries; PBX hostname still allowed) | Real `main()`/argparse with recording sentinel, not `--help` value proof | `tests/test_cli_validation.py` |
| Startup cancellation (cooperative discovery/tty/init checkpoints via optional `should_cancel`, 50 ms-sliced retry waits, no new reset/retry/USB thread after a checkpoint observes cancellation; `b` byte + 296-OPS positive control) | Software evidence only (fake clock is not physical timing; in-flight call not preempted; 10 s shutdown budget is not a total startup bound; interruption can leave firmware partial) | `tests/test_startup_cancellation.py` |
| systemd unit runtime (enable/start, automatic restart after USB loss, real `systemctl stop`), real-PBX contact-list absence and BYE receipt, broader PBX, second port, soak | **Not verified** (parser-level only on target udev 257.13: original rules `udevadm verify` rc 0 Success1/Fail0; original unit `systemd-analyze verify` rc 1 solely for missing `/opt/lineata/.venv/bin/python`, rc 0 with only the ExecStart interpreter substituted — syntax evidence, not installed-service acceptance; no activation/install/rule reload done) | — |

## 2. Why offline tests cannot prove these

Mock and synthetic fixtures exercise code paths only. They cannot prove device
firmware behavior: speech intelligibility, ring cadence and answer sequencing,
firmware state after a detach/replug, USB enumeration timing, or PBX behavior
beyond the mocked exchanges. Those need real hardware, a real PBX and an
operator — hence the ordered phases below.

Use physical checks at stable integration milestones, before building further
changes on an unverified hardware baseline. A short call regression should
cover inbound/outbound calls, two-way speech, both hangup directions and
successive calls. Record the exact commit, environment and per-check outcome;
test shutdown/restart separately so a failed call regression remains easy to
isolate. A passing offline suite is the prerequisite, not a replacement.

## 3. Ordered future validation

Each phase lists expected result, logs to retain, a time bound and stop
conditions. Run phases in order; do not start a later phase with an earlier
one unresolved.

### Phase 0 — silent Linux enumeration and permission checks (read-only, no device writes)

Prerequisite: prepare the reviewed code and venv at the paths in
[`deployment.md`](deployment.md), without enabling or starting the service.
The unit verifier also checks executable paths; a missing installed venv is
an installation prerequisite failure, rather than a unit-parser result.

- `systemd-analyze verify deploy/lineata.service` → expect no errors (the unit
  is still a draft; fix before any enable).
- Review `deploy/99-lineata.rules` scoping (vendor `31b7`, products `0001`/
  `0002`, `dialout` 0660) and `/etc/lineata.env` permissions (`root:root 0600`).
- With the dongle attached, read-only: first establish the USB mode from the
  device descriptor (`ls -l /dev/bus/usb/*/*` and the sysfs `idVendor`/
  `idProduct`) — bootloader mode is `31b7:0002`, service mode is `31b7:0001`.
  Then `ls -l /dev/ttyACM*` and run
  `python3 -c "from bridge.usb_identity import find_boot_tty; print(find_boot_tty())"`
  on the target. Expect exactly one boot tty (`/dev/ttyACM*`) **only in
  bootloader mode `31b7:0002`**; in service mode `31b7:0001` there is no boot
  tty and `find_boot_tty()` returning `None` is correct, not a failure. In
  bootloader mode, zero or multiple candidates fail closed (`None`).
- Expected: unit verifies clean; permissions match the rules; identity helper
  returns one unambiguous node. Retain: command outputs as text logs.
- Time bound: 15 min. Stop conditions: any verification error, ambiguous or
  missing tty, unexpected device nodes.

### Phase 1 — authorized cold boot + registration-only own-Contact removal

- Boot the dongle into service mode (authorized `b` boot path), confirm PID
  `0001` via read-only enumeration.
- Registration-only probe (secret prompted privately, never stored):
  `.venv/bin/python tools/sip_register_probe.py --pbx <PBX_IP> --login <EXTENSION>`
  → expect 200 OK; then deregister with explicit `expires=0` and confirm only
  our own Contact is removed (never a wildcard unregistration).
- Retain: structural report `python3 tools/audit_captures.py <capture>`
  (completion statuses, descriptor-confirmed service mode, incremental
  framing; it never prints audio or provisioning contents).
- Time bound: 10 min. Stop conditions: any auth failure loop, unexpected
  status codes, device reset.

### Phase 2 — operator-supervised physical calls (explicit authorization + operator present)

- Inbound: call the adapter extension from a test extension; expect ring,
  answer, two-way speech, remote hangup, later ONHOOK handled without repeated
  audio shutdown. Outgoing: OFFHOOK, digits, `#`, INVITE/200/ACK, two-way
  speech, ONHOOK → BYE/200. Then successive calls without reboot.
- Capture USB (usbmon / the `usbsnoop.c` shim) and `tcpdump -i any`; retain
  **partial USB reads** (PyUSB returns transferred bytes on timeout — never
  discard nonzero-transfer completions) and deduplicate mirrored RTP with
  `tools/audit_media.py` before measuring rates. Committed reports stay
  structural; raw captures with audio or SIP digest exchanges remain local and
  gitignored.
- Expected: behavior matching the `e0f3727` evidence, at ~50 frames/s.
- Time bound: 30 min. Stop conditions: operator unavailable, any repeated
  teardown, clock drift, clicking, or firmware reset.

### Phase 3 — detach/restart acceptance (only when expressly authorized)

- Unplug/replug the dongle and observe **automatic** process recovery via the
  unit's restart policy. Do NOT issue a manual service restart during this
  test — a manual restart masks a unit recovery failure. Only after an
  automatic-recovery failure is observed, record a separate manual-restart
  diagnostic; a manual restart is never acceptance evidence.
- Expected today: **process restart only** — there is no in-process reconnect.
  Graceful shutdown (SIGTERM → bounded teardown + own-Contact `expires=0`) is
  implemented, offline-tested and live-probed on the lab PBX (`deregister:ok`,
  own Contact absent from the final reply; see `docs/native-calls-2026-11-26.md`).
  Automatic service restart on the target and signal delivery with hardware
  attached remain unvalidated; record any observed behavior as new evidence.
- Retain: timestamps of detach, restart, first heartbeat, first successful
  REGISTER.
- Time bound: 20 min. Stop conditions: device fails to re-enumerate, start
  limit hit, any credential prompt.

## 4. Capture hygiene

Keep only structural/timing reports in version control (`tools/audit_captures.py`,
`tools/audit_media.py`): no audio payloads, no SIP digest exchanges, no
provisioning contents. Deduplicate mirrored RTP before rate measurements and
retain partial USB completions when reconstructing frame streams.

## 5. Global stop conditions

Stop all phases on: any unexpected device reset or bootloader fallback, any
permission or credential prompt that was not expected, loss of the operator,
or expiry of a phase time bound. Record what was observed before stopping.
