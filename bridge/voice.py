#!/usr/bin/env python3
"""Voice framing experiments on the Linear bulk transport.

The only unknown left in the protocol is how a live codec stream is framed
(chan messages on the FXS channel endpoints). From nvox's debug strings:
  "%s: send voice frame to USB: samples:%u, sub frames:%u"
  "FXS VRX: tx_first=%d tx_last=%d user_has_buf=%d usb_xfer=%d"
and the C++ classes ChanFxsTxMessage / PortFxsRxMessage, the stream uses
per-channel messages distinct from the 0x4x config family.

Strategy: sweep a candidate space of (host->dongle) frame shapes with
160-byte G.711 µ-law payloads (20ms @ 8kHz), observing:
 - which frames the device ACKs (cmd echo) vs stalls/errors on,
 - whether 0x40/0x60 poll replies grow extra payload (codec-attached?),
 - whether interrupt poll status bits change once a "voice arm" is sent.

This module is pure logic + driver: the sweep itself needs hardware.
"""
import time

SAMPLE_ULAW_SILENCE = bytes([0xFF] * 160)     # G.711u silence
SUBFRAMES = 1                                 # samples:160 sub_frames:1 first guess


class VoiceHypothesis:
    def __init__(self, name, frame_fn, expect="ack"):
        self.name = name
        self.frame_fn = frame_fn   # (port, payload) -> bytes out
        self.expect = expect

    def run(self, lin, port, payloads=8):
        sent = errors = acks = 0
        for i in range(payloads):
            frame = self.frame_fn(port, SAMPLE_ULAW_SILENCE)
            try:
                lin.w(frame)
                sent += 1
            except Exception as e:
                errors += 1
                if errors >= 3:
                    break
            time.sleep(0.02)
            try:
                r = lin.r(1000, 30)
                if r and r[0] == frame[0]:
                    acks += 1
            except Exception:
                pass
        return {"sent": sent, "errors": errors, "acks": acks}


def _chan_cmd(port, base):
    return base | 0x20 if port else base


# Candidate space. The 0x1x/0x2x range is where chan voice frames most
# plausibly live (0x4x=config paths, 0xa4/a5=device events observed).
HYPOTHESES = [
    VoiceHypothesis("hdr-port-len",
                    lambda p, d: bytes([_chan_cmd(p, 0x10), 0xA1, p]) + d),
    VoiceHypothesis("hdr-len-port",
                    lambda p, d: bytes([_chan_cmd(p, 0x12), 0xA1, p]) + d),
    VoiceHypothesis("hdr-samplesfields",
                    lambda p, d: bytes([_chan_cmd(p, 0x20), 0xA3, 160 & 0xFF, 160 >> 8, SUBFRAMES]) + d),
    VoiceHypothesis("hdr-subframes",
                    lambda p, d: bytes([_chan_cmd(p, 0x22), 0xA1, SUBFRAMES]) + d),
]


def sweep(lin, port, on_result=lambda n, r: None):
    results = {}
    for h in HYPOTHESES:
        r = h.run(lin, port)
        results[h.name] = r
        on_result(h.name, r)
    return results
