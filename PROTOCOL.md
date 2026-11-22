# Keenetic Linear (KN-3110) USB->FXS wire protocol

Reverse-engineered from `nvox` (`/usr/bin/nvox`, "Keenetic Phone Station",
nvox-0.8.2.96, MIPS32, musl) running on KeeneticOS 5.01.C.9.0-3 (KN-1810
Titan) and from live USB traces taken with the `usbsnoop.c` LD_PRELOAD
shim against nvox's raw usbfs ioctls. Traces are not published; the command
tables below summarize them.

## Hardware

- Dongle: EFM32 MCU (Silicon Labs) + **Si32287 ProSLIC** dual-channel FXS
  (Si32287FxsWrapper / Si32287TransportEfm32 in the nvox build tree).
- FXS firmware build date seen: `Feb 24 2021 16:37:28` (via cmd `0xc6`).
- Supports 2 analog ports; +DECT variants KN-3111/NC-3111 exist, same nvox.

## USB identities

| state | VID:PID | notes |
|---|---|---|
| cold / unbooted | `31b7:0002`, class 2 (CDC), bcdDevice 0.0 | enumerates as `USB CDC serial port device`, PRODUCT "Keenetic Ltd." |
| booted (nvox running) | `31b7:0001`, bcdDevice 0.44, iProduct `Keenetic Linear`, iSerial `[REDACTED]` | raw usbfs access, no kernel driver |

Serial format `[REDACTED]` (= S21xx + NS + number).

nvox opens `/dev/bus/usb/001/NNN` with libusb 0.1 (`libusb100` driver), claims
the interface, and does everything with bulk transfers + a few control
transfers. No CDC-ACM tty is used in booted mode.

**Endpoint map (booted mode)**

- EP `0x02` bulk OUT (64 bytes max observed 263? — control via SUBMITURB ep0; bulk max seen 162 bytes) — host -> dongle
- EP `0x81` bulk IN (host submits 1000-byte reads) — dongle -> host
- EP `0x00` control (SUBMITURB type 0): GET_DESCRIPTOR etc. only during enumeration.

## Frame format

Both directions: `[cmd][len][payload...]` where `len` = payload length.
Responses echo the host cmd byte first; a bare ack is `[cmd][02]`.
Total frame length = `len + 2`.

**Command bit 0x20 selects the FXS port**: `0x40`-series = port 0
("phone 1"), `0x60`-series = port 1 ("phone 2"). Same for `4e/6e`, `48/68`, etc.

## General commands (0x80..0xcf)

| cmd | payload host | payload dongle | observed semantics |
|---|---|---|---|
| `0x07` / `0x27` | `03 03` | ack | per-port shutdown/stop init step (late init, also seen at reset) |
| `0x80` | `03 01` | ack | init / power up channel (port pair A/B of 0x80/0xa0) |
| `0x81` / `0xa1?` | `03 00` | ack | per-site init after register setup |
| `0x82` | `06 14 0f 01 00` | ack | timing/init param block |
| `0xa0` | `03 00/01` | ack | tone gate / LED toggle driver (1 Hz cadence during dial tone) |
| `0xc0` | `03 00` | none (reap error `st=-39` typical) | FXS SET COUNTRY (index in payload); replies via error/no data |
| `0xc1` | `02` | `02` + version word | FXS GET FW VERSION |
| `0xc2` | `02` | `7a` + ASCII | device identity string: `z<17 digits>,ndds.keenetic.net,<token>,EU,KN-3110,Linear,<uuid-ish>` |
| `0xc6` | `02` | `16` + ASCII | FXS GET FW DATE (`%H:%M:%S %b %d %Y`) |
| `0xca` | `02` | (2) | unknown, called once after version checks |

## ProSLIC passthrough (per-port, 0x4x port0 / 0x6x port1)

| cmd | payload | reply | semantics |
|---|---|---|---|
| `0x41` | `addr16le, val16le` | ack | write SLIC register (addr byte pairs like `0d 13`, `03 ff`, `51 00`) |
| `0x48` | `addr16le` | `addr16le, val32le` | read SLIC register (`c1 01` -> `12 34 56 78` magic; hook/status regs `02 06`, `ff 02`, `03 00`, ...) |
| `0x45` | `addr16le, data` | ack | write RAM (seen 8B at addr `1a 04`, 20B at `52 10`) |
| `0x44` | `addr16le` | `addr16le + data` | read RAM (16B at `52 10` returns per-chip blob `b603a60fbf1075115907b8062305a711`) |
| `0x4a` | `addr16le, count16le` | `count*(addr..32bit)` | bulk RAM read: `4f 05 28 00` reads 40 words at 0x054f (codec coefficient blocks) |
| `0x4b` | `addr16le, count, ...` | ack | bulk RAM write: uploads codec coefficient tables (PCMU/PCMA/G.729/iLBC presets in `4f05` bank, `2300` block last) |
| `0x49` | `10B` | ack | RAM/reg write with extra args (pattern `c1 01 4c 00 78 56 34 12` = magic write) |
| `0x4e`/`0x6e` | `01 00, (addr16,val32)*` | ack | register-init batch; addr 0x0220 = **per-chip LB calibration** (differs per dongle — do not hardcode; read via `4a` at runtime) |
| `0x4c`/`0x6c` | `3d 06 08 00` | `count + (addr,val32)...` | read status block (interrupt source regs) |
| `0x4d`/`0x6d` | same + block | ack | write status block |
| `0x43`/`0x63` | `7e 04 02 08 0e 00` | ack | interrupt enable setup |
| `0x40`/`0x60` | `03 <flags>` | `03 <status>` | **event poll**: OUT payload byte flips between phases (`00`, `2f`, `03`, `0d`, `1d`, `7e`); IN payload = one status byte (`d3` idle, `00` after init, `8f` event pending, ...) |
| `0x47`/`0x67` | `31 77` | ack | port un/mute or linefeed write during call (seen repeatedly with voice paths) |

## Event messages (dongle -> host, unsolicited bulk IN)

| cmd | frame | semantics |
|---|---|---|
| `0xa5` | `a5 04 01 00` / `a5 04 00 00` | **hook change**: off-hook / on-hook |
| `0xa4` | `a4 04 <ASCII digit> 00` | **DTMF digit** pressed (ASCII: '5','4','3','2','1' observed at ~200 ms spacing) |
| `0x64` | `64 06 00 00 00 00` | port phys status block event |
| `0x65` | ack for host `0x65` writes | (host `65 08 1a 04 ...` RAM writes; ack `6502`) |

After a hook or digit event the host polls more registers (`48 04 ff 02`,
`48 04 03 00`, ...) to drain interrupt status (Si32287 IRQ regs) and re-arms
the poll.

## Init / boot sequence (replay)

From `captures/capture-call2.log` (line ~1..1220):

1. (Cold CDC state: send `boot\r\n` over the serial console — see Unknowns section — then re-enumerate to `31b7:0001`.)
2. `80 03 01`, `a0 03 01` — power up channels/init.
3. `40/60` polls with flag bytes `2f, 03, 0d, 1d, 7e` — bring-up state
   machine, interleaved with `41 04 0d 13 / 41 04 03 ff / 41 04 51 00` writes
   and `48/68` register reads,
4. `43/63 08 7e 04 02 08 0e 00` interrupt setup, `4d/6d` status block,
5. `4b 9e/92` + `4b a2 xN` — upload ~9 x 162-byte coefficient RAM blocks
   plus final 146-byte block (codec presets to RAM bank `0x054f`),
6. `49 0a ...`, `4a 06 4f 05 28 00` read-backs (verify),
7. `48 04` reads of ~14 calibration/option registers (`0x1052` RAM blob),
8. `4e/6e` register init batches (4 x 158-162B + small),
9. `82 06` + `81 03 00` + `c6/c1/c2` housekeeping, `c0 03 00` country,
10. `40 03 <flag>` idle poll ~10 Hz (flag `1d` in call-ready state).

Idle loop with device healthy: `40 03 1d`/`60 03 1d` -> `40 03 00`.

## Unknowns / TODO

- ~~Cold-boot trigger~~ **SOLVED (2026-11-23)**: cold CDC state runs an
  echoing line console on the CDC data interface (bulk 0x81/0x01); unknown
  lines answer `\r\n?\r\n`. The lowercase ASCII command `boot\r\n` makes the
  EFM32 boot the FXS app from flash and re-enumerate as `31b7:0001`
  (bcdDevice 0.44). No DTR/line-state, no firmware upload needed.
  Verified twice on macOS (see `bridge/linearusb.py cold_boot`).
- Ring generation sequence for incoming calls (never observed; expected in
  `4e`-batch form + cadence config via `phone.cadences`).
- Voice PCM framing (an active codec stream was never captured; expect
  `0x2x` channel messages per `ChanFxsTxMessage`/`PortFxsRxMessage` and the
  "S 2 ..." bulk frames during calls).
- Meaning of the `st=-39` urb status seen when nvox resets the FXS.

## Provenance

- `usbsnoop.c` — the LD_PRELOAD ioctl shim used for capture (build:
  `zig cc -target mipsel-linux-musleabi -O2 -shared -fPIC`)
- Reference daemon: nvox 0.8.2.96 (md5 `c7611011ac80286a9c5ae217a31799fa`, MIPS32 musl);
  build paths reveal tree `nvox-0.8.2.96/{base,fxs,cli,db,dect}/`
