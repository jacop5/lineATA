# lineATA — Keenetic Linear (KN-3110) as a generic USB→FXS adapter

Turn the Keenetic Linear dongle (EFM32 + Si32287 ProSLIC dual FXS) into a
USB FXS port for a SIP PBX, with no Keenetic router involved. FreePBX is
hardware-tested; UniFi Talk interoperability is planned and unverified.

## Status (2026-11-26)

Functional basic single-line ATA flow, verified with a KN-3110 and FreePBX.
The implementation remains experimental beyond that tested scope.

See the [implementation and testing roadmap](ROADMAP.md) for prioritized
feature gaps, release checks and work that requires a handset.

- Native boot/init, dial tone, inbound ring/answer, outgoing digits/INVITE,
  intelligible two-way speech, hangup from either side and successive calls
  are operator-confirmed on the Linux test host.
- The current USB scheduler passed a 20-second heartbeat-only hardware test.
  Control commands await matching replies; events remain dispatchable while
  waiting. Cancelled ring edges are removed before transmission.
- Registration and deregistration against the real FreePBX passed using the
  daemon's SIP listener, digest authentication and request builder.
- Graceful shutdown is implemented and offline-tested: SIGTERM/SIGINT set a
  flag-only request, then a bounded idempotent `shutdown()` (default 10 s)
  attempts to drain captured USB teardown frames, stops the registrar before a
  Contact-specific `REGISTER expires=0`, keeps the listener alive for the
  reply, and closes resources even on partial startup; requested exit is 0,
  fatal USB stop is 2. An unanswered incoming INVITE gets a correlated final
  480 (same To-tag as the 180) and no BYE before establishment. Mock and
  real-signal process tests only — no live PBX validation (see
  `docs/reliability.md`).
- Startup honors cancellation cooperatively: discovery/tty/init checkpoints
  stop on a combined stop/request check, retry waits poll in 50 ms slices, and
  no new reset, retry or USB thread is started after a checkpoint observes cancellation. An in-flight
  native call is not preempted, and the 10 s shutdown budget does not bound
  total startup time.
- The missing `270300` audio-enable command and accumulating media-clock
  drift were corrected. Hardware USB/RTP media now runs near 50 frames/s;
  the operator confirmed the regular clicking disappeared. A brief transient
  already present in the final USB audio frame at analog hangup remains open.
- Voice frames observed on USB are `00 a2` + 160 bytes at about 20 ms.
  Native PCMU passthrough is verified by intelligible speech and matching
  captured USB/RTP payloads.

Start with [the hardware validation](docs/native-calls-2026-11-26.md).
`AUDIT.md` indexes the current findings and the preserved historical reports.
[Software reliability evidence and the offline test guide](docs/reliability.md)
summarize the reviewed software tests and their limits; the systemd deployment
in `deploy/` and `docs/deployment.md` is an **unvalidated offline draft**.

## Layout

- `bridge/ata.py` — current USB/SIP/RTP daemon
- `bridge/transport.py`, `framing.py`, `media.py` — control queue and wire parsers
- `bridge/capture_init.py` — observed 296-operation initialization
- `tools/audit_captures.py`, `usbmon_parse.py`, `audit_media.py` — raw capture
  and media-timing audits, including partial USB reads and mirrored RTP
- `tools/sip_register_probe.py` — registration-only check, no USB or calls
- `artifacts/experiments/` — bounded hardware experiments, including a silent probe
- `PROTOCOL.md` — wire observations plus explicitly historical semantic guesses
- Other bridge modules and `linearctl.py` — legacy experiments, not the current daemon
- `asterisk/` — PBX container config (docker-compose + pjsip/extensions)
- `usbsnoop.c` — LD_PRELOAD ioctl shim used to capture usbfs traffic on the
  router (build: `zig cc -target mipsel-linux-musleabi -O2 -shared -fPIC`)

## Development and use

Python 3.10+ and a libusb backend are required. On Debian, install
`python3-venv` and `libusb-1.0-0`; USB access must be granted to the daemon user.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The suite includes 13 historical tests that are not evidence for the current
daemon. Current regressions are in `test_native_transport.py`,
`test_control_queue.py`, `test_sip_lifecycle.py` and `test_usbmon_parse.py`.
Media timing and capture-mirror regressions are in `test_media_pacing.py`
and `test_audit_media.py`. Registration, USB lifecycle and graceful-shutdown
regressions live in `test_registration*.py`, `test_usb_*.py`,
`test_boot_tty_selection.py`, `test_shutdown*.py` (including real OS-signal
process tests with faked USB/SIP startup).
The real UDP loopback test is opt-in:

```sh
LINEATA_LOOPBACK_TEST=1 .venv/bin/python -m pytest tests/test_rtp_loopback.py -q
```

Registration-only check (prompts privately for the extension secret):

```sh
.venv/bin/python tools/sip_register_probe.py --pbx PBX_IP --login EXTENSION
```

For the daemon, set `LINEATA_SIP_PASSWORD` in the service environment and run:

```sh
.venv/bin/python -m bridge.ata --pbx PBX_IP --local-ip HOST_IP --login EXTENSION
```

CLI arguments are validated before the ATA is constructed: `--pbx-port` must be
1-65535, `--sip-port` 0-65535 (`0` keeps an ephemeral local port), the dial
timeout must be finite and positive, and `--local-ip` must be an IPv4 literal —
wildcard, multicast, limited-broadcast, hostnames and IPv6 are rejected. The
parser never queries DNS or interfaces and does not infer address assignment or
routing class (e.g. `240.0.0.1` parses fine); `--pbx` may still be a hostname.
The secret flag/environment priority is unchanged.

Negotiated outbound in-call DTMF has software packet tests and a real FreePBX
IVR passes for 0-9*#, adjacent repeats and a two-second held-key check.
Event duration remains fixed nominal100ms; broader PBX coverage is pending. See the [DTMF guide](docs/dtmf.md).
The default is RFC 4733 when negotiated; use --dtmf-mode inband to disable
named events and retain PCMU-only signaling. No in-band tone filter is applied.

The daemon can ring the phone on incoming calls. Supported scope currently
targets a single direct IPv4 SIP/UDP PBX with MD5 digest and RTP/AVP PCMU.
Two-port operation, SIP routing through proxies, re-INVITE/hold, other digest
algorithms, TLS/SRTP and NAT traversal are not validated.

Local evidence captures remain outside version control. Reproduce their
structural report with `python3 tools/audit_captures.py PATH_TO_CAPTURE`.
The report checks USB completions, descriptor-confirmed service mode and
incremental framing, without printing audio or provisioning contents.

## Target deployment

Proxmox host → USB passthrough of the dongle to a Debian VM →
bridge daemon registers its FXS ports as SIP endpoints (initially to a
co-located Asterisk; UniFi Talk later via generic SIP or a trunk).

## License / provenance

Reverse-engineering for interoperability. Contains no Keenetic code or
firmware images — only observed USB traffic, decoded command tables, and
original client code. All tracing was done on the author's own hardware.
