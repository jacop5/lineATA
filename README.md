# lineATA — Keenetic Linear (KN-3110) as a generic USB→FXS adapter

Turn the Keenetic Linear dongle (EFM32 + Si32287 ProSLIC dual FXS) into a
USB FXS port for any PBX (Asterisk / UniFi Talk on Proxmox), no Keenetic
router involved.

## Status (2026-11-23, Phase A)

- USB wire protocol fully decoded from Keenetic's `nvox` daemon
  (`PROTOCOL.md`)
- Cold-boot trigger discovered: CDC serial console, `boot\r\n`
- Working macOS client: cold boot → libusb bulk → FW/identity queries
- Asterisk 22 PBX container (OrbStack) with endpoints 101/102 + echo 600
- Voice PCM framing and ring generation: pending (see PROTOCOL.md TODO)

## Layout

- `PROTOCOL.md` — the decoded wire protocol (start here)
- `bridge/` — the daemon core (`linearusb.py`: boot/init/poll, event decode)
- `linearctl.py` — standalone client (same library, CLI convenience)
- `asterisk/` — PBX container config (docker-compose + pjsip/extensions)
- `usbsnoop.c` — LD_PRELOAD ioctl shim used to capture usbfs traffic on the
  router (build: `zig cc -target mipsel-linux-musleabi -O2 -shared -fPIC`)

## Quick start (new dongle bring-up on any host)

```sh
python3 bridge/linearusb.py boot     # cold CDC → Linear mode (31b7:0002 → :0001)
python3 bridge/linearusb.py info     # FW date/version/identity
python3 bridge/linearusb.py poll     # 10 Hz poll, prints hook/DTMF events
```

Dockerized PBX (OrbStack / any docker): `docker compose -f asterisk/docker-compose.yml up -d`

## Target deployment

Proxmox host → USB passthrough of the dongle to a Debian VM →
bridge daemon registers its FXS ports as SIP endpoints (initially to a
co-located Asterisk; UniFi Talk later via generic SIP or a trunk).

## License / provenance

Reverse-engineering for interoperability. Contains no Keenetic code or
firmware images — only observed USB traffic, decoded command tables, and
original client code. All tracing was done on the author's own hardware.
