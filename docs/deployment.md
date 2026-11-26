# lineATA deployment draft (OFFLINE — review only, nothing installed)

Status: **draft for review**. No service was installed, enabled or started; no
udev rules were loaded; no device passthrough was modified. All commands below
are instructions for the target Debian host, not actions taken here.

## Tested scope vs. unverified behavior

Tested (hardware, operator-confirmed, `docs/native-calls-2026-11-26.md`):
basic **single-line** call flow on one KN-3110 against the lab FreePBX —
boot/init replay, dial tone, inbound ring/answer, outgoing digits/INVITE,
two-way PCMU speech, hangup from either side, successive calls, `270300`
media enable, 20 ms deadline pacing (~50 frames/s).

Also live-checked on the Proxmox host at commit `d0c2e2a` (real Linux, real
dongle, lab PBX): sysfs boot-tty identity resolved to `/dev/ttyACM0`, cold
boot `31b7:0002 → 31b7:0001`, all 296 init operations acknowledged, SIP
REGISTER confirmed (299 s lease), operator-confirmed inbound call (ring,
two-way speech, ONHOOK disconnect) and outbound call — **both hangup directions
passed**: a remote BYE left the analog handset silent (see
`docs/native-calls-2026-11-26.md`). A live USB-removal run was followed by a
confirmed PBX reply to the `REGISTER expires=0` and daemon exit; a live probe
of the real registrar + real `shutdown()` then passed (rc 0) with
`deregister:ok` and own Contact absent from the final REGISTER reply —
absence observed in that reply's Contact list, not via an independent
registrar/PBX-database query (see `docs/native-calls-2026-11-26.md`).

Explicitly **not validated** (do not claim otherwise):

- **In-process USB reconnect/recovery.** If the dongle disappears or the fatal
  USB handler fires, the current daemon exits (`bridge/ata.py` `usb_loop` sets
  `stop`); it does not re-open the device. The unit below therefore restarts
  the whole process (see "Restart policy"), which cold-boots the dongle —
  that path is exercised only at process start, never mid-session. A live
  USB-removal run at `d0c2e2a` showed the deregistration reply and daemon exit;
  automatic restart via the unit on the target host is still **not**
  validated (no service was installed or enabled).
- **Graceful shutdown is implemented, offline-tested and live-probed.**
  `main()` installs SIGTERM/SIGINT handlers whose body only sets a shutdown
  request event (no I/O, joins or teardown in the handler). The wait loop then
  runs `shutdown()` (default budget 10 s, monotonic deadline, idempotent —
  repeated calls add no duplicate final traffic): captured state-appropriate
  USB frames are queued and the queue drained for at most 1 s, the registrar is
  stopped and joined **before** a Contact-specific `REGISTER expires=0`
  (never a wildcard removal) is sent, the SIP listener stays alive to receive
  the removal reply, pending outgoing INVITEs get a bounded cancellation grace
  (at most 1 s within the overall budget), and resources are closed even on a
  missing reply or partial startup. Exit status: requested shutdown exits 0;
  a fatal USB stop exits 2. Offline evidence: the OS-signal tests
  (`tests/test_shutdown_process.py`) drive the real `main()` with faked USB/SIP
  startup. Live evidence: the real registrar + real `shutdown()` probe against
  the lab PBX passed (rc 0) with `deregister:ok` and own Contact absent from
  the final reply (see above). Signal delivery with hardware attached and
  service-managed restart remain unvalidated. `TimeoutStopSec=25`/
  `KillSignal=SIGTERM` in the unit still only bound the kill from systemd's
  side.
- Second jack, long soak, other PBX implementations, TLS/SRTP, NAT, re-INVITE.
- The brief hangup transient in the final USB audio frame (open issue).
- **No in-process USB reconnect; process restart is not target-validated.**
  Cold-boot/initialization now honors a cancellation request cooperatively:
  the startup helpers take an optional `should_cancel=None`, and
  connect/init/run pass a combined stop/request check, so discovery, tty and
  init checkpoints stop, retry/startup waits poll in 50 ms slices, owned
  resources are disposed, and no new reset, retry or USB thread is started
  after a checkpoint observes cancellation. The positive control is unchanged: the `b` boot byte and
  the 296-operation init replay. Limits: fake-clock measurements are software
  evidence, not physical timing; an in-flight native call cannot be preempted;
  the 10 s shutdown budget does not guarantee a total startup bound; and an
  interruption can leave the firmware partially initialized, so an actual
  restart/cold boot after an interruption still needs authorized hardware
  validation. Software evidence only: `tests/test_startup_cancellation.py` and
  the startup-cancellation regression tests.

## Target layout and paths

- Code + venv: `/opt/lineata` with `/opt/lineata/.venv` (Python 3.10+).
  The unit uses **absolute paths only** (`WorkingDirectory=/opt/lineata`,
  `ExecStart=/opt/lineata/.venv/bin/python -m bridge.ata ...`), so the service
  never depends on the caller's cwd or relative paths.
- Unit: `/etc/systemd/system/lineata.service` (from `deploy/lineata.service`).
- Config/secret: `/etc/lineata.env`, **root-owned, mode 0600** (example:
  `deploy/lineata.env.example`, placeholders only — no real addresses/secrets,
  and no inline comments after values: systemd preserves such text as part of
  the value, so comments live on their own lines).
- udev: `/etc/udev/rules.d/99-lineata.rules` (from `deploy/99-lineata.rules`).
- User: system user `lineata` (no login shell, no home), group `dialout`.

## Required dependencies (Debian target)

- `python3` (3.10+), `python3-venv`, `python3-pip`
- `libusb-1.0-0` (system library required by PyUSB)
- Python package: `pyusb==1.3.1` (from `requirements.txt`) into the venv
- `systemd`, `udev`
- USB access is granted by the targeted udev rules (vendor `31b7`, products
  `0001`/`0002`) plus `dialout` membership — no broad USB permissions.

## Install (minimal, reproducible)

```sh
# 1. dependencies
sudo apt-get install -y python3 python3-venv python3-pip libusb-1.0-0

# 2. non-root service user with dialout only
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin lineata
sudo usermod -aG dialout lineata

# 3. install REVIEWED COMMITTED sources only, from the repo checkout:
#    `git archive HEAD` excludes the gitignored raw captures/, .env secrets,
#    /.orchestration/ runtime state and .re-env/.venv, which `cp -a .` would
#    have copied. The reviewed commit must contain deploy/* and this guide;
#    archive-based installation excludes all uncommitted working-tree edits.
sudo mkdir -p /opt/lineata
git archive HEAD | sudo tar -x -C /opt/lineata
sudo /usr/bin/python3 -m venv /opt/lineata/.venv
sudo /opt/lineata/.venv/bin/pip install -r /opt/lineata/requirements.txt
sudo chown -R root:root /opt/lineata   # code read-only to the service user

# 4. secret config: root-owned 0600, real values entered by hand
sudo install -o root -g root -m 0600 deploy/lineata.env.example /etc/lineata.env
sudoedit /etc/lineata.env   # LINEATA_PBX, LINEATA_PBX_PORT, LINEATA_LOGIN,
                            # LINEATA_LOCAL_IP, LINEATA_SIP_PORT, LINEATA_SIP_PASSWORD

# 5. targeted USB permissions
sudo install -o root -g root -m 0644 deploy/99-lineata.rules /etc/udev/rules.d/99-lineata.rules
sudo udevadm control --reload-rules
# replug the dongle, or: sudo udevadm trigger --attr-match=idVendor=31b7

# 6. unit (enable+start only when actually deploying)
sudo install -o root -g root -m 0644 deploy/lineata.service /etc/systemd/system/lineata.service
sudo systemctl daemon-reload
sudo systemctl enable --now lineata.service
```

Configuration permissions: `/etc/lineata.env` is `root:root 0600` and read only
by PID 1 via `EnvironmentFile=`; the daemon receives `LINEATA_SIP_PASSWORD` in
its environment and never gets the secret on its command line (visible in
`ps`). `/opt/lineata` is root-owned and read-only to the `lineata` user
(`ProtectSystem=strict` in the unit); the service user needs no write access
anywhere — the daemon only logs to stdout/journald.

## Removal

```sh
sudo systemctl disable --now lineata.service
sudo rm /etc/systemd/system/lineata.service /etc/udev/rules.d/99-lineata.rules /etc/lineata.env
sudo systemctl daemon-reload
sudo udevadm control --reload-rules
sudo rm -rf /opt/lineata
sudo userdel lineata
```

## Service behavior (review notes for `deploy/lineata.service`)

- Foreground `Type=simple`, `ExecStart=/opt/lineata/.venv/bin/python -m
  bridge.ata` with PBX/login/local-IP/SIP-ports taken from `/etc/lineata.env`
  via `${...}` expansion; nothing host-specific is hardcoded in the unit. The
  flags used are real CLI flags of `bridge/ata.py` (`--pbx`, `--pbx-port`,
  `--login`, `--local-ip`, `--sip-port`; verified against `--help` in
  `tests/test_deployment_config.py`). Argument **values** are validated before
  the ATA is constructed by the real `main()`/argparse with a recording sentinel
  in `tests/test_cli_validation.py` (not `--help` value proof): `--pbx-port`
  1-65535, `--sip-port` 0-65535 (`0` keeps an ephemeral port), finite positive
  dial timeout, and `--local-ip` must be an IPv4 literal (wildcard, multicast,
  limited-broadcast, hostname and IPv6 rejected); the parser does not query
  DNS or interfaces and does not infer address assignment or routing class
  (e.g. `240.0.0.1` parses). `--pbx` may still be a hostname; the secret
  flag/env priority is unchanged.
- **Restart policy:** `Restart=always` + `RestartSec=10` (paced retries in
  `[Service]`) + `StartLimitIntervalSec=0` in `[Unit]` (start-limit directives
  belong to `[Unit]`). Rationale: the daemon's fatal USB handler exits with
  status 2 and an unexpected clean exit is also possible, so `Restart=always`
  covers both; with the start limit disabled, a dongle reconnected at any
  later time can still be picked up by a restart. An explicit
  `systemctl stop lineata` remains stopped — systemd never restarts a unit
  after a manual stop. This restart behavior is **not validated on a target
  systemd host**.
- `KillSignal=SIGTERM`, `TimeoutStopSec=25`: SIGTERM now reaches the daemon's
  graceful shutdown (bounded, default 10 s, deregistration included); the unit
  values only bound how long systemd waits before SIGKILL. Parser-checked on
  the target (see "Validation status"); runtime behavior on a real unit is
  still not validated.
- No `DeviceAllow`/passthrough changes: USB access relies on the udev rules +
  `dialout`. On a Proxmox VM, USB passthrough of the dongle is a host-side
  decision outside this draft; nothing here modifies it.

## USB boot-device selection

Cold boot now uses Linux sysfs identity discovery (`bridge/usb_identity.py`).
The daemon requires exactly one ttyACM node whose USB ancestor carries both
vendor `31b7` and bootloader product `0002`. Missing metadata, missing device
nodes, no match or multiple matches prevent the boot byte from being sent.
It no longer selects an unrelated first ttyACM or guesses a macOS usbmodem.
An already running service-mode device does not need a boot tty.

Temporary-directory fixtures cover this selection policy, and real Linux
sysfs discovery plus cold boot were exercised live at `d0c2e2a` (identity
helper returned `/dev/ttyACM0`; device then enumerated as `31b7:0001`).
Multiple dongles and non-Linux cold boot remain unsupported; use a dedicated
Linux host or VM and validate enumeration before service activation.

## Validation status of this draft

Hardware facts were reviewed against `bridge/ata.py` at e0f3727 (CLI flags,
`LINEATA_SIP_PASSWORD` env default, fatal-handler behavior); the software
rework (registration, USB lifecycle, graceful shutdown) is reviewed at commit
`cf6a367` and later working-tree changes pending commit. Offline regression
tests for the draft live in `tests/test_deployment_config.py` (unit structure,
env-file value syntax, CLI flag/int/IP compatibility via `--help` and argparse
rejection, udev rule scoping) and pass on the review host's Python. Root ran
parser checks on the real Proxmox host (systemd/udev 257.13; no activation,
install, rule reload or device access): `udevadm verify deploy/99-lineata.rules`
→ **rc 0 (Success 1, Fail 0) on the original rules**; `systemd-analyze verify
--man=no deploy/lineata.service` on the **original unit** → rc 1 with the sole
finding the missing installation prerequisite `/opt/lineata/.venv/bin/python`;
a temporary copy differing **only** in the ExecStart interpreter
(`/usr/bin/python3`) verified rc 0. This is syntax evidence only — **not**
installed-service acceptance. Service runtime (enable/start, automatic restart after USB loss, real
`systemctl stop` behavior) is **not validated on Linux**; run the full unit through systemd on the
target before relying on it.
