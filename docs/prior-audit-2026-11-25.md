# Independent audit of the previous agent's work (2026-11-25, successor session)

Method: re-decoded all `captures/2026-11-25/*.pcap` from raw bytes with
`tools/usbmon_parse.py` (no trust in prior summaries), diffed `capture_init.py`
against the warm capture, and read all bridge code. Hardware was NOT touched
(no dongle on this host); all findings below are capture-derived.

## Verified as correct

- Frame format `[type][len][payload]` with **len = total frame length** (voice
  `00 a2` + 160 bytes; events `a5 04 xx 00`). (PROTOCOL.md says len = payload
  length — that is wrong, fixed.)
- Event frames `a5 04 <hook> 00`, `a4 04 <ascii> 00` — confirmed byte-exact in
  `nvox-warm-events.pcap` (OFFHOOK 4.735, DTMF 7.115/8.318/9.923, ONHOOK 12.845).
- Init replay: `capture_init.OPS` is a 296/296 **ordered byte-exact subsequence**
  of the warm capture's host→device frames. The capture only adds a few extra
  frames (e.g. `c00300` country set) around it. Solid.
- Voice: `00 a2` + 160 bytes µ-law both directions at 20 ms; silence = `0xff`
  → PCMU confirmed. RTP passthrough design is correct.
- Boot: single byte `b` on the CDC console (matches latest git commit).

## Incorrect assumptions / false positives in HANDOFF.md

1. **"Ring crash root cause unknown; suspect missing reg 0x1e = 0x44
   prerequisite"** — refuted. In `nvox-inbound-call-audio.pcap` NOTHING was
   sent on USB between t=4.33 (0x1e reads 0x11) and t=6.33 (0x1e reads 0x44).
   What actually happened at t=4.423: nvox sent `6e0a0002168000001e04` and
   `4e0a0002168000001e04` — an **incoming-call announce** that writes
   reg 0x1e = 0x04 (reads then show 0x44 = 0x40 app-flag | 0x04). Our daemon
   never sent this frame → app not in call state → ring writes crash it.
   This is the most likely ring-crash cause; fix applied in `ata.py`
   (`CALL_ANNOUNCE` sent before ringing and again before answer, matching
   t=4.423 and t=7.076).
2. **"Never write port-2 (4e…) ring frames while port 1 is active"** — false
   positive. nvox sends BOTH `6e0e…` and `4e0e…` ring frames on every ring
   edge (t=6.327/6.330/6.729/6.731/7.074/7.995) without crashing. The earlier
   crashes correlate with the missing announce frame, not with port-2 frames.
3. **Port decoding `port = data[1] - 0x04`** (native.py, flagged in
   HANDOFF §9.6) — wrong: `data[1]` is the length byte (always 0x04), so this
   always yielded port 0. Captured a5/a4 frames contain **no port field**;
   fixed to assume the single active port.
4. **ANSWER sequence was missing the `6e12 00 00 0c 09 00 02 0d 03 00 00 0e 09
   00 02 0f 03` frame** (capture t=7.137, between `21 06…` and `66 04 0b 10`).
   Added to `ANSWER`.
5. **HANGUP sequence was wrong**: observed ONHOOK-during-call hangup is
   `27 03 03`, `21 06 00 00 00 02`, `03 02`, `67 04 31 77`, `67 04 0b 10`
   (t=16.111–16.116) — not `["67043177","61040b10"]`. Fixed.
6. **"Dial-tone frame is one fixed 94-byte blob"** — questionable: after the
   2nd DTMF digit nvox sent a *different* 94-byte `6e5e…` frame (differs in the
   tone-generator parameter bytes). Verbatim replay is ACKed but per-digit
   variants exist; flagged in code comment.
7. **PROTOCOL.md "len = payload length"** — wrong per every capture (len
   includes the 2 header bytes). Doc fixed.
8. **200 OK to INVITE echoed the caller's Contact header** — wrong; now sends
   our own Contact.

## Invalid tests / tooling notes

- `tests/test_fsm.py::test_initsequence_shape` fails here only because pyusb
  is not installed on this host (`import usb`); the other 12 tests pass.
- usbmon text parsing caveat from HANDOFF §8.9 confirmed: do analysis on the
  pcap via `usbmon_parse.py`, not the .txt awk fields.

## Unexplored hypotheses (for the next hardware session, in priority order)

1. Ring without crash after `CALL_ANNOUNCE` — the fix above; verify on device.
2. ONHOOK during media: events may be lost while `00 a2` frames flow —
   `--debug-frames` flag added to `ata.py` to log raw IN frames during media.
3. Caller-ID frame `05 21 …` (contains number + name, e.g. "69"/"Test
   macbook") and IN event `86 04 00 00` (call-waiting?) — unimplemented.
4. Meaning of `sub frames:2` (160 B = 2×10 ms?) — test 80-byte halves only if
   audio sounds choppy.
5. `0x44` vs `0x11` on reg 0x1e reads = app-state flag | host-written nibble.

## Changes made (offline; nothing sent to hardware)

- `bridge/ata.py`: CALL_ANNOUNCE before ring/answer; full ring cadence with
  per-edge 0x1e polls and both-port ring frames at observed 400/350 ms; fixed
  ANSWER (added `6e12…`) and HANGUP sequences; BYE stops ringing + hangup
  frames; 200 OK carries our Contact; `--debug-frames` logging.
- `bridge/native.py`: removed bogus port decode.
- `PROTOCOL.md`: corrected frame-length semantics; marked legacy claims.
