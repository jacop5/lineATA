# Keenetic Linear (KN-3110) USB->FXS wire protocol

Current evidence and limitations: [native call validation](docs/native-calls-2026-11-26.md)
and [independent takeover audit](docs/reaudit-session-2026-11-25.md).
Several older semantic interpretations below remain historical hypotheses.
Use `bridge/ata.py` for the current daemon; the legacy register sweeps are
not a validated bring-up procedure.

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

## USB identities (corrected via libndmNvox.so strings 2026-11-24)

| PID | identity (ndm/nvox names) |
|---|---|
| 31b7:0002 | **Keenetic Linear Bootloader** - firmware bootloader (the "CDC console" state: string table, XMODEM-capable, waits for host boot/start) |
| 31b7:0001 | Keenetic Linear (FXS service app) |
| 31b7:0004 | Netcraze Linear Bootloader (KN-3111 variant family) |
| 31b7:0003 | Netcraze Linear |

nvox (via libndmNvox / FxsBootlogger) recognizes the bootloader PIDs
(NVOX_FXS_BOOTLOADER_IDS), and ndm spawns
`nvox --config /var/run/nvox-fxs-boot.conf --boot -d <tty>` on hotplug of
a bootloader-state dongle; that flow boots the FXS app with the full
service personality (run-status, events a5/a4/64 flow). The `-u`/`--update`
path uploads firmware over the tty using XMODEM-CRC (C-ping / ACK/NAK /
chunk CRC / EOT) with expectation strings "000002FF" (KN-3110 CRC),
"0000B077"/"000076F1" (KN-3111/NC-3111) and version ladders.

The ACTUAL nvox
`--boot` boot command is a SINGLE BYTE `b` (no newline, no terminator),
written raw to /dev/ttyACMx - confirmed by disassembling the nvox CLI
-boot handler (VA 0x4073e8-0x4073fc: logger(3,"Boot FXS firmware") then
write(fd, 0x60A054="b", 1) then close(fd) on success). Single-byte `b`
boots the event-capable service app in the native tests. An earlier claim
that `boot\r\n` selects a distinct maintenance personality is not established
by the available evidence; a string beginning with `b` does not itself
demonstrate a separate firmware code path.

## USB identities (legacy notes)

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

Both directions: `[cmd][len][payload...]` where **`len` = total frame length
including the 2 header bytes** (verified against captures: voice frame is
`00 a2` + 160 bytes, len 0xa2 = 162; event `a5 04 01 00` has len 4 = 4 bytes
total). Responses echo the host cmd byte first; a bare ack is `[cmd][02]`.
(An earlier revision of this section said len = payload length — wrong.)

**Command families differ by bit 0x20**: `0x40`/`0x60`, `4e/6e`, `48/68`.
Their physical jack numbering has not been independently established.
The captured phone uses `6e` for tone/answer; oracle and native ringing use
both families. Captured hook events have a final zero byte; the second
jack's event encoding is still unverified.

## General commands (0x80..0xcf)

| cmd | payload host | payload dongle | observed semantics |
|---|---|---|---|
| `0x07` | `03 03` | ack | historical shutdown/init observation; broader semantics unverified |
| `0x27` | `03 00` / `03 03` | ack | `270300` enables handset→host audio after answer; `270303` appears in init/teardown; broader mask semantics unverified |
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

## Runtime behaviors & gotchas (from live experimentation)

- **Init watchdog ~15s**: the FXS app self-resets if the host does not
  complete the bring-up within `fxs.init_timer` (nvox default 15000 ms).
  A replay that takes 16.5 s faults right at the first polls; the stock
  init completes in ~2.7 s. Keep replay well under 12 s.
- **Steady poll flag is `0x1d`** (`40 03 1d` -> `40 03 00`). Polling with
  flag `0x00` is only valid during the captured startup state walk and
  crash-stops a steady session (endpoint halt -> device reset).
- **`81 03 00` (STOP)** halts the app in some states - never write it on
  a live session; the captured init uses it only at the very beginning of
  a fresh bring-up.
- **`boot\r\n` is app-handled, not ROM**: once the app has halted (from
  asserts), the CDC console is dead and only a full power cycle recovers.
- **Assert replies**: invalid/unsolicited general commands are answered
  with a short frame carrying ASCII assert info from the fw's own
  `commands.c` (observed: `c1 0e {02 "commands.c...` then USB halt).

## Steady-state heartbeat

Current native tests use alternating `a00301`/`a00300` at 1 Hz after the
296-frame initialization, without register polling or periodic init-block
replay. A 20-second test of the current runtime queue received all 20 ACKs.
Older claims below describe a different implementation and do not establish
that this larger cycle is required:

Idle operation is NOT bare polling: the host must run the steady cycle
captured at trace ops 84..115 - alternating port polls interleaved with
per-port sync blocks (`4ea2/4e1a`, `6ea2/6e1a`, hook-register reads
`48 04 02 06`, IRQ drain reads, RAM-mute markers). Repeating this cycle
indefinitely holds the FXS app in service indefinitely (tested >100 s
with `400390/600390` port states and zero faults); polling alone is
reaped by the app watchdog after ~5-15 s.

Implementations: `initctl.STEADY_CYCLE` + `initctl.steady_tick()`.

## Unknowns / TODO

- Cold boot: send single byte `b` via the dongle's CDC tty, then wait for
  `31b7:0001`; tested natively on the Linux test host. Automatic selection of
  the correct tty among multiple CDC devices still needs improvement.
- ~~Ring generation sequence~~ **OBSERVED 2026-11-25** (see AUDIT.md and
  `captures/2026-11-25/nvox-inbound-call-audio.pcap`): on INVITE, host sends
  `6e0a0002168000001e04` + `4e0a0002168000001e04` (incoming-call announce,
  reg 0x1e := 0x04); then per ring edge it polls `60031e`/`40031e` and sends
  `6e0e0002500400001e0100015004` + `4e0e…` (ring on, ~400 ms), and the
  `…1e02…` variants for ring off (~350 ms). Answer (offhook): announce again,
  then `67043177`, `61040b01`, `210600010100`,
  `6e1200000c0900020d0300000e0900020f03`, `66040b10`. Hangup (ONHOOK):
  `270303`, `210600000002`, `0302`, `67043177`, `67040b10`.
- Media enable: about 200 ms after the answer sequence, oracle sends
  `270300`. Its omission left native handset→host audio absent. Adding it
  enabled two-way speech in controlled native incoming and outgoing calls.
  Do not treat the five answer operations alone as complete media setup.
- Voice wire framing is observed: `00 a2` plus 160 bytes, about every 20 ms
  in both directions. PCMU passthrough is now verified by intelligible native
  speech and matching captured USB/RTP payloads. A final USB audio spike at
  analog hangup remains an audio-quality issue.
- Meaning of the `st=-39` urb status seen when nvox resets the FXS.

## Provenance

- `usbsnoop.c` — the LD_PRELOAD ioctl shim used for capture (build:
  `zig cc -target mipsel-linux-musleabi -O2 -shared -fPIC`)
- Reference daemon: nvox 0.8.2.96 (md5 `c7611011ac80286a9c5ae217a31799fa`, MIPS32 musl);
  build paths reveal tree `nvox-0.8.2.96/{base,fxs,cli,db,dect}/`
