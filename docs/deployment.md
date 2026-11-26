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

Explicitly **not validated** (do not claim otherwise):

- **End-to-end USB reconnect/recovery.** If the dongle disappears or the fatal
  USB handler fires, the current daemon exits (`bridge/ata.py` `usb_loop` sets
  `stop`); it does not re-open the device. The unit below therefore restarts
  the whole process (see "Restart policy"), which cold-boots the dongle —
  that path is exercised only at process start, never mid-session.
- **SIGTERM / graceful shutdown is entirely unimplemented.** `main()` wraps
  its wait loop in `try/except KeyboardInterrupt/finally`; **no signal handler
  is installed for SIGTERM**, so on SIGTERM the OS default action terminates
  the process immediately and the `finally` block (`stop.set()`,
  `call.hangup()`, `sip_sock.close()`) does **not** run — only Ctrl-C/SIGINT
  reaches it. No exit path sends `REGISTER expires=0` (deregistration) or a
  device teardown sequence either. `TimeoutStopSec=25`/`KillSignal=SIGTERM` in
  the unit therefore only bound the kill; a graceful path still needs
  implementation and integration testing.
- Second jack, long soak, other PBX implementations, TLS/SRTP, NAT, re-INVITE.
- The brief hangup transient in the final USB audio frame (open issue).

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
sudo useradd --system --no-create-home --shell /usr/sbin/nologin lineata
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
  `tests/test_deployment_config.py`).
- **Restart policy:** `Restart=always` + `RestartSec=10` (paced retries in
  `[Service]`) + `StartLimitIntervalSec=0` in `[Unit]` (start-limit directives
  belong to `[Unit]`). Rationale: the daemon's fatal USB handler can end the
  process with a **clean** exit status, so `Restart=on-failure` would not
  cover it; `Restart=always` covers unexpected clean exits too. With the start
  limit disabled, a dongle reconnected at any later time can still be picked
  up by a restart. An explicit `systemctl stop lineata` remains stopped —
  systemd never restarts a unit after a manual stop.
- `KillSignal=SIGTERM`, `TimeoutStopSec=25` anticipate the graceful-shutdown
  integration (deregistration) that is **still to be implemented** — see
  "unverified" above.
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

Temporary-directory fixtures cover this selection policy; real Linux sysfs
and cold-boot integration remain unvalidated after this change. Multiple
dongles and non-Linux cold boot are unsupported; use a dedicated Linux host
or VM and validate enumeration before service activation.

## Validation status of this draft

Reviewed against `bridge/ata.py` at e0f3727 (CLI flags, `LINEATA_SIP_PASSWORD`
env default, fatal-handler behavior). Offline regression tests for the draft
live in `tests/test_deployment_config.py` (unit structure, env-file value
syntax, CLI flag/int/IP compatibility via `--help` and argparse rejection,
udev rule scoping) and pass on the review host's Python. `systemd-analyze
verify` and udev rule validation were **not run**: the review host is macOS
with no systemd/udev tooling and installing validators was out of scope. The
unit and rules are therefore **not validated on Linux**; run
`systemd-analyze verify lineata.service` on the target before enabling.
