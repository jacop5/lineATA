# HANDOFF — LineATA (Keenetic Linear KN-3110 → generic USB→FXS ATA)

> Historical report. Consult [AUDIT.md](AUDIT.md) for independently verified
> findings and corrections before using these conclusions or procedures.

Date: 2026-11-25
Author: previous agent session (Opus/GPT-5 class), successor: Codex
Repository: `/Users/jac/git/lineATA`
Remote test host: `<TEST_HOST>` (root SSH; password **not** recorded here — held by the
operator; referenced below as `<SSH_PASSWORD>`)
PBX: FreePBX 17.0.33.4 / Asterisk 22.10.1 at **10.9.12.136**, extension `<EXT>`
(secret held by the operator, referenced as `<EXT_SECRET>`). Second test endpoint
`69` (“Test macbook”) was used as the far end.

> Read this before touching the dongle. Several experiments in this document
> **crash the FXS app** (device re-enumerates to PID 0x0002 or disappears for a few
> seconds). That is expected and recoverable.

---

## 1. Objective

Make a **Keenetic Linear KN-3110** USB dongle usable as a **USB-to-FXS ATA for a
generic PBX** (Asterisk/FreePBX), with a **native driver** — explicitly **not** by
running Keenetic’s `nvox` under qemu in production (operator rejected that).

Success criteria as understood:
1. Native host daemon talks to the dongle over USB (no Keenetic hardware, no nvox).
2. Registers to the PBX over SIP.
3. Outgoing: FXS phone goes off-hook → dial tone → digits → INVITE.
4. Incoming: INVITE → FXS port rings → answer → 2-way audio.
5. Hangup both ways.

---

## 2. Architecture

```
   analog phone / DECT base
            │ FXS
   ┌────────▼─────────┐   USB bulk   ┌──────────────────────────────┐
   │ KN-3110 dongle   │◄────────────►│ native daemon (Python)       │
   │ Si32287 ProSLIC  │  EP 0x02 OUT │  bridge/ata.py               │
   │ + EFM32 MCU app  │  EP 0x81 IN  │   ├─ USB mailbox protocol    │
   │ (KM3 “FW 0044”)  │  + CDC-ACM   │   ├─ SIP UA (REGISTER/INVITE)│
   └──────────────────┘              │   └─ RTP PCMU passthrough    │
                                     └──────────────┬───────────────┘
                                                    │ SIP/RTP (UDP)
                                              FreePBX 10.9.12.136
```

Key idea that unlocked everything: **the USB payload is already G.711 µ-law**, and
PCMU RTP is µ-law — so audio is a straight repack (160 bytes ↔ one RTP packet), no
transcoding. This also means Python 3.13’s removal of `audioop` is irrelevant.

`nvox` (Keenetic’s own daemon) is used only as an **oracle**: it is run under
`qemu-mipsel` against the real dongle, its USB traffic is captured with `usbmon`,
and its byte sequences are replayed/stored. The product path is `bridge/ata.py`.

---

## 3. Current status (facts)

| Capability | State | Evidence |
|---|---|---|
| Cold boot 0x0002 → 0x0001 via CDC `'b'` | **works** | repeated runs; `nvox -b` capture `captures/2026-11-25/nvox-boot-path.*` |
| Full init replay (296 frames) | **works** | log line `usb: armed`; device stable ≥50 s |
| Hook events (OFFHOOK/ONHOOK) | **works natively** | `EVENT hook OFFHOOK`, `ONHOOK` (job log, see §7.1) |
| DTMF digits | **works natively** | `EVENT dtmf '1' '2' '3'` |
| Dial tone command | **sent + ACKed** (`6e02`); audibility not conclusively confirmed by operator in the native path | capture + logs |
| SIP REGISTER to FreePBX | **works** | `SIP/2.0 200 OK`; Asterisk sends OPTIONS keepalives to our contact |
| Asterisk OPTIONS keepalive answer | **implemented** | needed, otherwise peer shows unreachable |
| Outbound INVITE → 69 | **worked once** (100/180/200, call rang and was answered) | §7.3 |
| Outbound INVITE later attempts | **timeout** (stale call on PBX / no BYE sent) | §7.4 |
| Inbound INVITE → ring dongle | **FAILS**: ring frames crash the FXS app | §8 |
| 2-way audio through our stack | **NOT verified**; audio frame counters never reached media in test windows | §8 |
| Hangup (ONHOOK → BYE) | **NOT working in our stack** (no ONHOOK delivered during the one connected call) | §7.3 |
| Voice framing known | **yes** (see §5.7) — proven with nvox inbound call, 2-way audio operator-confirmed | capture `nvox-inbound-call-audio.pcap` |

Bottom line: **protocol is decoded end-to-end; the native daemon implements it, but
two app-state problems remain: ring crashes the app, and the ONHOOK event did not
arrive during a connected call.**

---

## 4. Hardware and environment

### 4.1 Dongle
- Keenetic Linear **KN-3110**, 2 FXS ports, USB powered.
- USB IDs: `31b7:0002` = bootloader (CDC-ACM console, XMODEM-capable),
  `31b7:0001` = FXS service app (1 config, interface 0: bulk IN `0x81`, bulk OUT
  `0x02`, plus CDC-ACM which also exists in 0x0001).
- Chip: **Si32287 rev D** ProSLIC + **EFM32** MCU (from nvox strings:
  `PORT%d: unsupported chip detected (%u), we expect chip SI32287`,
  `We expect SI32287 revision D (0x3)`; source paths
  `fxs/si32287/Si32287TransportEfm32.cpp`).
- App identity: `FXS Si32287 9.2.0 HW S2145NS000385 FW 0044 built on 17:28:49 Feb 24 2021`.
- Serial: `S2145NS000385`.
- Operator’s DECT base is on **port 1**.

> ⚠️ Early work in this repo (and `PROTOCOL.md` sections about Si3226x registers)
> used the **wrong chip family**. The Si3226x register map is not the Si32287 map.
> All conclusions derived from Si3226x docs are invalid for this device. The wire
> frame format below is correct because it comes from observed traffic.

### 4.2 Test host `<TEST_HOST>`
- Debian x86_64, Python **3.13.5**, `qemu-mipsel` + `qemu-mipsel-static`,
  `tcpdump`, `strace`, `unsquashfs`, root access.
- Dongle enumerated on **USB bus 1** (`/sys/kernel/debug/usb/usbmon/1u` is
  **unreadable** — `Operation not permitted`; **`tcpdump -i usbmon1` works**).
- `usbmon` module loaded; `modprobe usbmon` already done.
- CDC node: `/dev/ttyACM0` (bootloader mode definitely; service mode also creates it).

### 4.3 qemu / nvox oracle setup on `<TEST_HOST>`
```
/tmp/nvox                      Keenetic nvox 0.8.2.96, MIPS32 LE, musl, stripped
/tmp/mipsroot/lib              ld-musl-mipsel-sf.so.1, libssp.so.0, libgcc_s.so.1, libatomic.so.1
/tmp/mipsroot/usr/lib          libusb-1.0.so.0, libsupc++.so, libssl.so.3, libcrypto.so.3
/tmp/kn1810.bin                KN-1810_stable_5.01.C.9.0-3 firmware (squashfs at offset 1966080)
/tmp/knrootfs                  unsquashed rootfs (source of the libs + nvox)
```
Run: `qemu-mipsel -L /tmp/mipsroot /tmp/nvox -c <conf> [--debug]`
Boot app first (otherwise the daemon prints `No USB dongle.`):
`qemu-mipsel -L /tmp/mipsroot /tmp/nvox -c <conf> -d /dev/ttyACM0 -b`
(device must be in `0x0002`; `-d` takes the **full path**; a bare `ttyACM0` is
opened relative to CWD and fails).

### 4.4 nvox config that is known to work (no credentials here)
```
dect.pincode=1234
fxs.country=RU              # RU is the only country value confirmed accepted
db.locale=en
files.path=/tmp/nvfiles     # must exist; also files.active_calls=, files.history_calls=, files.phonebook=
app.loglevel=5              # valid range 1..5
sip.udp_port=5060
sip.auto_init=1
sip.ip_address=<TEST_HOST_IP>
sip.lines[0].enabled=1
sip.lines[0].id=1
sip.lines[0].login=<EXT>
sip.lines[0].password=<EXT_SECRET>
sip.lines[0].domain=10.9.12.136
sip.lines[0].proxy=sip:<EXT>@10.9.12.136;transport=udp;lr
sip.lines[0].reg_uri=sip:<EXT>@10.9.12.136
```
Validated parser facts:
- format is flat `key=value`; **no INI sections** (`[x]` → `Malformed setting`).
- `sip.lines[0].login/password/domain` together are required; with `.login` but
  without `.domain` nvox dies with `sip.line.login is not empty` /
  `fatal error in LineConfig.cpp:96`. Adding `.domain` yields
  `SIP pjsip 2.6 status=started` then `registration success, 200 OK`.
- **invalid** keys (each aborts with `Unknown config parameter`/`Unknown array or
  field`): `sip.lines` (count), `sip.line.enabled`, `sip.line.login`,
  `sip.line.digit_map`, `sip.lines[0].bind`, `fxs.ports[0].line`,
  `fxs.ports[0].sip_id`, `sip.profiles[1].login`.
- valid: `fxs.ports[N].enabled`.
- `sip.lines[0].digit_map=...` is **accepted but has no effect** (dialing still
  rejected with “did not match any digit map”; the digit map is applied at runtime
  by the router’s RPC, not from the file).
- nvox writes `LINE%d login=%s domain=%s bind=%s pub_addr=%s` and
  `FXS port %d is detached from lines` exist in the binary.

---

## 5. Reverse-engineered protocol (all evidence-backed)

Source of truth: `captures/2026-11-25/*.pcap` + `*.txt` (text = output of
`tools/usbmon_parse.py`, which decodes LINKTYPE_USB_LINUX_MMAPPED=220).

### 5.1 Framing
Every bulk transfer (both directions) is one or more frames:
```
[type byte][len byte][payload …]        len = TOTAL frame length (incl. type+len)
```
Voice is the exception that proves the rule: `00 a2 <160 bytes>` → type=0x00,
len=0xa2 (162) — the generic splitter works for everything.

### 5.2 Command bytes observed (host → device, EP 0x02)
| cmd | meaning (observed) | reply |
|---|---|---|
| `0x40/0x41` | read/write direct register, port 1 | `40 03 <v>` / `41 02` |
| `0x60/0x61` | same, port 2 | `60 03 <v>` / `61 02` |
| `0x44` | physical/line detect query | `64 06 …` |
| `0x48/0x49` | read/write indirect (register file), port 1 | `48 06 <v32>` / `49 02` |
| `0x68/0x69` | same, port 2 | |
| `0x4a` | read RAM/poll | `4a …` |
| `0x4b` | patch/RAM block load (162-byte chunks `4b a2 4f05 27 0000 …`) | `4b 02` |
| `0x4c/0x4d/0x4e` | bulk ops; `0x4e` = multi-register write as `{val16,addr16}` pairs, port 1 | `4e 02` |
| `0x6e` | same as 0x4e for port 2 | `6e 02` |
| `0x64` | physical-status read/command (`64 04 12 04` in init) | `64 06 00 00 00 00` |
| `0x65` | `65 08 16 04 00 00 00 00` (during init) | `65 02` |
| `0x66/0x67` | direct writes used at answer/hangup | `66 02` / `67 02` |
| `0x27` | `27 03 03` | `27 02` |
| `0x80/0x81/0x82` | service commands (`80 03 01` FXS init/start, `81 03 00` stop, `82 06 14 0f 01 00` config) | `80 02`, `81 02`, `82 02` |
| `0xa0` | 1 Hz heartbeat toggle `a0 03 01` / `a0 03 00` | `a0 02` |
| `0xc0/0xc1/0xc2` | letter/config commands from nvox (country/firmware/identity); **`c1 03 01` kills our app — do not send** | — |

### 5.3 Events (device → host, EP 0x81, unsolicited)
```
a5 04 01 00   OFFHOOK on port 1
a5 04 00 00   ONHOOK  on port 1
a4 04 31 00   DTMF '1' (0x31; '2'=0x32 … '* '=0x2a, '#'=0x23)
64 04 …       physical/plug detect (e.g. 64 04 12 04)
```
Observed at (nvox warm run, relative seconds): OFFHOOK 4.279, DTMF 1 6.659,
DTMF 2 7.862, DTMF 3 9.467, ONHOOK 12.389.
Port-2 variants use `a5 04 …` too but with a different length/port field seen as
`a5 04 01 00` after `6e`-prefixed traffic in one session; treat port decoding as
“first payload byte 0x01 = port 1, 0x00 = on-hook”.

### 5.4 Idle behaviour of nvox (important)
- Heartbeat: `a0 03 01` / `a0 03 00` alternating **1 Hz**.
- Additionally, nvox **polls `40 03 1d` and `60 03 1d` every ~10 ms** while idle
  (reply `40 03 00`/`60 03 00` = ready; `… 8f` = busy).
  ⚠️ Our driver doing the same 10 ms poll **kills the app after ~5 s**
  (see §8.6). Do not copy the poll rate blindly.

### 5.5 Tones / call control (verbatim from capture)
```
dial tone (sent ~7 ms after OFFHOOK), 94 bytes:
6e5e0002317701001a0000008f0701001b000000260001001c00000000000000331f000032400000
35000000340001001d000000000801001e000000000001001f000000000000003700000036000000
3900000038000000304600013101

stop tone:            67 04 31 77
answer (inbound):     67 04 31 77
                      61 04 0b 01
                      21 06 00 01 01 00
                      6e 12 00 00 0c 09 00 02 0d 03 00 00 0e 09 00 02 0f 03
                      66 04 0b 10

ring ON  (port 1):    6e 0e 00 02 50 04 00 00 1e 01 00 01 50 04
ring OFF (port 1):    6e 0e 00 02 50 04 00 00 1e 02 00 01 50 04
ring ON  (port 2):    4e 0e 00 02 50 04 00 00 1e 01 00 01 50 04
ring OFF (port 2):    4e 0e 00 02 50 04 00 00 1e 02 00 01 50 04
```
Ring cadence observed in nvox: ~400 ms on / ~350 ms off at the start of ringing,
and nvox sends the **port-1 and port-2** frames around each ring edge, each
preceded by a read of `reg 0x1e` (`60 03 1e` / `40 03 1e`). It also sends
`6e 0a 00 02 16 80 00 00 1e 04` just before answering.

### 5.6 Boot path (CDC console)
- `c` (0x63) → reply `63`, `\r\n`, `000002FF`.
- `b` (0x62) → app boots, device re-enumerates as `31b7:0001` (~1–3 s).
- `nvox -b` does exactly this and exits; the daemon must be started separately.
- Kill/exit of nvox **resets the dongle** (device disappears briefly).

### 5.7 Voice (proven with an operator-confirmed 2-way call under nvox)
Both directions, every **20 ms**, 162 bytes:
```
00 a2 <160 bytes G.711 µ-law>
```
- Silence in the capture is `ff` (µ-law) → **µ-law (PCMU), not A-law**.
- RTP: PT 0, ptime 20, 160-byte payloads — direct passthrough.
- nvox logs `send voice frame to USB: samples:160, sub frames:2`; the exact meaning
  of “sub frames:2” and of the two header bytes (`00`, `a2`) is **not** proven
  (see §9). Both ports’ audio appears to use the same header on port 1.

### 5.8 Register observations (Si32287) — limited, do not over-trust
- `reg 0x1d` reads `00` = ready, `8f` = busy (used as init wait-loop).
- `reg 0x1e` read `44` in nvox **before** the ring write, `11` **after**; our app is
  `11` at idle and never reaches `44`, even after 12 s of polling (it crashed first).
- `reg 0x0b` reads `00`.
- The Si3226x register map (PD_DCDC etc.) from earlier work **does not apply**.

---

## 6. Repo contents (state as handed off)

```
PROTOCOL.md              earlier protocol notes (⚠️ contains Si3226x-based claims that are wrong)
README.md                project overview (older status text)
bridge/
  ata.py                 ★ native daemon: USB + SIP UA + RTP + ring/answer  (primary work)
  native.py              ★ minimal native driver: init + heartbeat + events + dial tone (proven)
  capture_init.py        ★ 296-frame init, byte-exact from nvox usbmon capture (used by ata.py)
  initsequence.py         317-op init from the earlier Titan golden capture (used by /tmp/origtest.py)
  linearusb.py, daemon.py, initctl.py, events.py, handlers.py, ring.py, voice.py,
  sipside.py, main.py, arming.py, regmap.json   ← earlier (Si3226x-era) implementation; superseded,
                                                  kept for reference, several assumptions are wrong
tools/
  usbmon_parse.py        ★ usbmon pcap decoder (LINKTYPE 220), prints S/C with payloads
  capture_diff.py, cdc_probe.py
tests/  test_events.py test_fsm.py test_framing.py  (older design; not updated)
linearctl.py, usbsnoop.c, asterisk/ (docker PBX from earlier phase)
captures/                older shim logs (capture-call2.log, golden-call.log, …)
captures/2026-11-25/     ★ this session’s pcap + decoded text (see below)
artifacts/remote-scripts/ ★ credential-free test scripts pulled from <TEST_HOST>
```

### 6.1 Captures (this session)
| file | content |
|---|---|
| `captures/2026-11-25/nvox-warm-events.pcap` (+`.txt`) | nvox warm session; at 4.279 OFFHOOK, 6.659/7.862/9.467 DTMF 1/2/3, 12.389 ONHOOK; includes the 94-byte dial-tone frame and `67 04 31 77` |
| `captures/2026-11-25/nvox-inbound-call-audio.pcap` (+`.txt`) | ★ inbound call from ext 69 to `<EXT>`: ring frames at 6.423/6.825/7.170, answer at 7.231–7.235, **2-way µ-law audio** 7.34–15.4 (operator confirmed audio both ways) |
| `captures/2026-11-25/nvox-boot-path.pcap` (+`.txt`) | `c` checksum + `b` boot over CDC bulk, re-enumeration |
| `captures/2026-11-25/nvox-failed-outbound.pcap` (+`.txt`) | session where our driver transmitted nothing; shows a *previous* session’s `*43` digits and our OFFHOOK/ONHOOK with no DTMF |

Decode any of them with:
`python3 tools/usbmon_parse.py <pcap> [--dev N] [--ep 81]`

### 6.2 Remote artifacts still on `<TEST_HOST>`
Credential-free scripts (copied into `artifacts/remote-scripts/`):
`ringtest.py`, `ringtest2.py`, `ackinit.py`, `poll10.py`, `origtest.py`, `watch1e.py`.
Scripts that **embed the PBX secret** and were deliberately NOT copied
(regenerate if needed): `iter5.sh`…`iter10.sh`, `iterx.sh`, `iterz.sh`, `dmt.sh`,
`sipcall.sh`, `sipreg.sh`, `callcap.sh`, `callcap2.sh`, `inccap.sh`, `inccap2.sh`,
`coldcap.sh`, `readafter.sh`.
Also on host: `/tmp/linbridge/` (deployed `ata.py`, `native.py`, `capture_init.py`,
`initsequence.py`), `/tmp/nvox`, `/tmp/mipsroot`, `/tmp/knrootfs`, `/tmp/kn1810.bin`,
`/tmp/nvfiles/`, `/tmp/usb.*` pcaps, `/tmp/nv.*` strace logs.
`/tmp/nvox.conf` on the host **contains the PBX secret** — do not copy it anywhere.

---

## 7. Confirmed findings (with evidence)

### 7.1 Native driver receives events (no nvox)
`bridge/native.py` (init from `capture_init.py`, 1 Hz `a0` heartbeat, 4-byte event
parser) produced, during a 50 s run with the operator dialing:
```
init: replaying 296 frames
init: armed
running: pick up the phone
EVENT hook port1 OFFHOOK
EVENT dtmf port1 '1'
EVENT dtmf port1 '2'
EVENT dtmf port1 '3'
EVENT hook port1 ONHOOK
```
This is the proof that the mailbox protocol is fully understood for control.

### 7.2 nvox drives the dongle perfectly under qemu (oracle works)
Operator-observed: dial tone, DTMF, and an **inbound call with 2-way audio**.
Logs: `FXS Si32287 9.2.0 … FW 0044`, `SIP pjsip 2.6 status=started`,
`registration success, status=200 OK`, `send voice frame to USB: samples:160,
sub frames:2` every 20 ms.

### 7.3 Native ATA: outbound call once connected end-to-end
With `bridge/ata.py` (REGISTER + INVITE + digest + RTP):
```
19:33:00 EVENT dtmf '6'
19:33:00 EVENT dtmf '9'
19:33:03 dialing 69
19:33:03 sip: SIP/2.0 100 Trying
19:33:03 sip: SIP/2.0 180 Ringing
19:33:07 sip: answered
```
Operator: ext 69 rang and the call was answered. **No audio either way**, and
hanging up the handset did not end the call on 69 (no ONHOOK in the log → no BYE).

### 7.4 SIP registration details
- `REGISTER` → `401` (Digest, realm "asterisk") → authed `REGISTER` → `200 OK`.
- Asterisk then sends `OPTIONS` to our Contact; **answering it with 200 is
  required** for the peer to show reachable. Implemented in `ata.py`.
- A later INVITE timed out (no response) — most likely because the previous call
  was never torn down (no BYE) and/or FreePBX state.
- Our UA must bind a fixed UDP port (default 5062 in `ata.py`) and advertise it in
  Contact/Via.

### 7.5 Boot/init timing
- `reset_to_bootloader()` → PID 0x0002; `boot_app()` writes `b` to `/dev/ttyACM0`,
  waits for PID 0x0001, sleeps 2.5 s.
- Init replay uses the captured inter-frame delays; the `40 03 1d`/`60 03 1d`
  frames are **wait loops** — repeat until the reply value is `0x00`.
- Init sometimes fails once (`init failed at 60031d` or `c102`) and succeeds after a
  cold reboot; `ata.py` now retries up to 5 times with a device reset.

---

## 8. Failed experiments (exact outcomes) — do not repeat blindly

### 8.1 Si3226x-era manual register work (obsolete)
Earlier session treated the chip as Si3226x, wrote `PD_DCDC`, `DCDC_CPUMP` etc.
Consequences: contradictory values, app resets (`ENODEV`), false “DC/DC powered”
conclusions. **All of it is void for Si32287.** The vendor API source used
(`proslic_api-v5.6.4L`, Realtek tree) is the wrong chip.

### 8.2 `c1 03 01` (version query)
Every attempt: no reply, then the app is dead (`Errno 19`). `c1` is not a valid
command for this app. Do not send it.

### 8.3 Fast replay of the full init
Fast replay (no per-frame delay) of the 296-frame init leaves the app in a bad
state and drops frames. Use the captured delays and ACK checking.

### 8.4 Ringing with replayed nvox ring frames
- `6e0e0002500400001e0100015004` (port 1) is accepted (ACK `6e02`).
- **Any subsequent ring frame** (port-2 `4e0e…`, or the next cycle's `60031e`,
  or the ring-off) → device disappears (`Errno 19`).
- Same crash with 2 s of prior polling; with port-2 frames removed; with the
  original 317-op init; with ACK-verified replay.
- Root cause **not** established. Suspects, in order: (a) line feed / ring power
  state never reached because the app is not told about the call the way nvox
  does it; (b) a missing register/config pre-requisite (`reg 0x1e` reads `11`
  where nvox had `44`); (c) our poll/response discipline differs from nvox’s.

### 8.5 Continuous `0x1d` polling at 10 ms
Borrowed from nvox, but with our driver the app **dies after ~5 s even with no
ring command** (`poll10.py` output: 4 good seconds of `p1=600311 p2=400311`,
then `Errno 19`). Do not poll at 10 ms.

### 8.6 Inbound call with our daemon
`ata.py` received the INVITE, sent 100/180, started the ring cadence; ext 69 showed
ringing; **the FXS phone never rang** and the app crashed. See §8.4.

### 8.7 Outbound call with our daemon — media
Call connected, but the audio counters were never observed (test window ended
before/at answer), so the media path is unverified. The RTP relay code and the
USB voice frame code are written but unproven in our stack.

### 8.8 nvox config dead ends
- `sip.lines=1`, `sip.line.*` flat keys, `sip.lines[0].bind`,
  `fxs.ports[0].line`, `sip.profiles[N].*` → parse errors (see §4.4).
- `digit_map` in the config file is ignored; dialing is rejected.
- `app.loglevel=6` → out of range (1..5).
- `files.path` must be an existing dir with the three file names configured.
- Starting the daemon straight from bootloader → `No USB dongle.`; run `-b` first.

### 8.9 Capture tooling
- Reading `/sys/kernel/debug/usb/usbmon/1u` as root → `Operation not permitted`.
- `tcpdump -i usbmon1 -w file.pcap -s 0 -U` works.
- Parsing caveat: for `C IN` records the `st=   0` contains a space, shifting awk
  fields — payload length is `$10` for IN, `$9` for OUT in the parsed text.
- usbmon pcap is LINKTYPE_USB_LINUX_MMAPPED (220), 64-byte header; OUT payload is
  in the `S` (submit) record, IN payload in the `C` (complete) record.

---

## 9. Unverified assumptions (explicitly NOT facts)

1. `0xa2` in the voice header is a length/type; `0x00` may be a channel or sequence
   byte. Only port-1 audio was captured.
2. `sub frames:2` from nvox — whether 160 bytes is one port’s 20 ms, or two 10 ms
   subframes, is unresolved. If audio sounds wrong/choppy, test 80-byte halves.
3. µ-law is inferred from `0xff` silence and the PCMU RTP mapping; A-law would show
   `0xd5`. Confirm by ear (should be intelligible) or by comparing captured bytes
   with a known µ-law encoder.
4. Meaning of `reg 0x1e` values `0x44` vs `0x11`; why our app never reaches `0x44`.
5. Whether the ring crash is caused by a missing “incoming call” announcement, a
   missing country/tone preset, or line-power state.
6. That `a4 04 …`/`a5 04 …` port decoding is `data[1] - 4` as implemented
   (`ata.py` uses `port = data[1] - 0x04`); only port 1 was exercised.
7. That the 94-byte dial-tone frame is universally valid (it was replayed verbatim
   from one capture); audibility with our driver was not confirmed by the operator.
8. That the ONHOOK event simply never arrived during the connected call; it could
   also have been lost by our framing loop while audio was flowing.

---

## 10. Blockers and next tasks (ranked)

1. **Make ringing work without crashing the app.**
   Highest-value next experiment: get nvox’s *exact* app-state transition for an
   incoming call. Two concrete options:
   a. Capture a cold nvox session where the daemon itself boots the app and then
      receives an INVITE (all frames from the first byte), and diff against our
      init — look specifically for anything that sets `reg 0x1e` to `0x44`.
   b. Capture nvox’s USB traffic while it answers an inbound call and compare the
      `0x1e` read *before* the ring write; if the `0x44` comes from a command we
      drop, replay that command first.
   Also test whether the ring must be preceded by an inbound-call announcement
   (there is a `6e 0a 00 02 16 80 00 00 1e 04` frame right before answer).
2. **Hangup**: ensure ONHOOK is received during a connected call; log raw frames
   while `media` is true. If the app stops emitting events during media, poll the
   hook state via a *slow* register read instead (do not use 10 ms polling).
3. **Verify audio** in our stack: after the 200 OK, count `00 a2` frames both ways
   (counters already in `ata.py`) and confirm the far end hears the handset.
4. **Stability**: replace the 10 ms poll with the 1 Hz heartbeat only (proven 50 s
   stable) or a much slower poll (≥100 ms) if polling is needed at all.
5. **Cleanup**: `PROTOCOL.md`/`README.md`/`bridge/*` still carry Si3226x-era claims;
   mark them obsolete so the next agent does not follow them.

---

## 11. Exact reproduction instructions

### 11.1 Last known good: native control-plane (events)
```bash
# on <TEST_HOST>, as root
cd /tmp/linbridge
python3 -u native.py --cold
# -> "init: replaying 296 frames", "init: armed", then EVENT hook/dtmf lines
```
Files needed in `/tmp/linbridge`: `native.py`, `capture_init.py`.
Cold boot sequence inside: reset to PID 0x0002 → write `b` to `/dev/ttyACM0`
→ wait for 0x0001 → 2.5 s → replay 296 frames with captured delays and
`0x1d` wait-loops → 1 Hz heartbeat → parse `[type][len]` frames from EP 0x81.

### 11.2 Last known good: full SIP path with nvox (oracle, not a product)
```bash
# 1) device in bootloader (unplug/replug or usb reset)
# 2) boot the app with nvox
qemu-mipsel -L /tmp/mipsroot /tmp/nvox -c /tmp/nvox.conf -d /dev/ttyACM0 -b
# 3) run the daemon
qemu-mipsel -L /tmp/mipsroot /tmp/nvox -c /tmp/nvox.conf --debug
# 4) from another phone call <EXT>; the FXS port rings, answer, 2-way audio works
```
`/tmp/nvox.conf` as in §4.4 (with real credentials entered locally, never committed).

### 11.3 Native ATA daemon (current state: registers, outbound works once)
```bash
cd /tmp/linbridge
python3 -u ata.py --login <EXT> --password <EXT_SECRET> \
    --pbx 10.9.12.136 --local-ip <TEST_HOST_IP> \
    --sip-port 5062 --dial-timeout 3
```
Expected log: `usb: armed`, `sip: starting on …`, `sip: registered as <EXT>`.
Dial from the handset after the dial tone; digits collect for 3 s then INVITE.
**Known failures:** no ring on inbound; no BYE on hangup; audio unverified.

### 11.4 Capture USB traffic for any new experiment
```bash
timeout 60 tcpdump -i usbmon1 -w /tmp/cap.pcap -s 0 -U &
# ... run the test ...
python3 tools/usbmon_parse.py /tmp/cap.pcap > /tmp/cap.txt
grep -E "S OUT ep2" /tmp/cap.txt | awk '$1+0>2 {print $1,$9,substr($10,1,80)}'
grep -E "C IN  ep1" /tmp/cap.txt | awk '$10+0>100'      # voice frames
```

### 11.5 Recover a wedged dongle
Unplug/replug, or:
```python
import usb.core, time
d = usb.core.find(idVendor=0x31b7)
if d: d.reset(); time.sleep(3)
# then boot with 'b' as above
```

---

## 12. Rules of engagement (learned the hard way)

- Never send `c1 03 01`.
- Never write port-2 (`4e…`) ring frames while port 1 is active.
- Never poll `0x1d` at 10 ms.
- Treat any `Errno 19` as “app crashed”: reset, cold boot, re-init; do not retry
  the same frame.
- The Si3226x register map is wrong for this chip.
- The USB payload is µ-law; do not transcode unless proven necessary.
- Keep every capture; they are the only ground truth.
