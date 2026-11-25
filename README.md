# lineATA — Keenetic Linear (KN-3110) as a generic USB→FXS adapter

Turn the Keenetic Linear dongle (EFM32 + Si32287 ProSLIC dual FXS) into a
USB FXS port for any PBX (Asterisk / UniFi Talk on Proxmox), no Keenetic
router involved.

## Status (2026-11-25)

Experimental single-line daemon. A functional end-to-end ATA is still the goal.

- Native boot/init and a prepared USB ring pulse work on the Linux test host;
  the operator confirmed physical ringing. This was a bounded USB experiment,
  not an end-to-end incoming SIP call through the updated daemon.
- The current USB scheduler passed a 20-second heartbeat-only hardware test.
  Control commands await matching replies; events remain dispatchable while
  waiting. Cancelled ring edges are removed before transmission.
- Registration and deregistration against the real FreePBX passed using the
  daemon's SIP listener, digest authentication and request builder.
- Two-way synthetic RTP byte preservation passed over real UDP loopback.
  A combined SIP/USB/RTP simulation also passed two successive calls with
  local and remote hangup, using a synthetic USB device.
  Native handset speech, incoming answer, both hangup directions and repeat
  calls still need end-to-end validation.
- Voice frames observed on USB are `00 a2` + 160 bytes at about 20 ms.
  PCMU is the current codec hypothesis; USB/RTP codec equivalence has not
  been independently established by matching real call bytes.

Start with [the current audit](docs/reaudit-session-2026-11-25.md).
`AUDIT.md` indexes the current findings and the preserved historical reports.

## Layout

- `bridge/ata.py` — current USB/SIP/RTP daemon
- `bridge/transport.py`, `framing.py`, `media.py` — control queue and wire parsers
- `bridge/capture_init.py` — observed 296-operation initialization
- `tools/audit_captures.py`, `usbmon_parse.py` — reproducible raw capture audit
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
