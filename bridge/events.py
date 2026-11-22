#!/usr/bin/env python3
"""Typed decoding of Linear dongle -> host event frames."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class HookEvent:
    port: int          # 0 or 1
    offhook: bool

@dataclass
class DtmfEvent:
    port: int
    digit: str

@dataclass
class PhysEvent:
    raw: bytes         # 0x64 phys status block

@dataclass
class PollStatus:
    port: int
    status: int        # one status byte from 0x40/0x60 poll reply

@dataclass
class Unknown:
    cmd: int
    payload: bytes


def port_of(cmd: int) -> int:
    return 1 if (cmd & 0x20) else 0


def decode(frame: bytes):
    """Decode one dongle->host frame into a typed event.

    Frame layout: [cmd][len][payload...]; see PROTOCOL.md.
    """
    if len(frame) < 2:
        return Unknown(frame[0] if frame else -1, frame)
    cmd = frame[0]
    ln = frame[1]
    payload = frame[2:2 + ln]
    if cmd == 0xa5:                                   # hook: [state, port]
        state = bool(payload[0]) if payload else False
        port = payload[1] if len(payload) > 1 else 0
        return HookEvent(port=port, offhook=state)
    if cmd == 0xa4:                                   # DTMF: [ascii digit, port]
        if not payload:
            return Unknown(cmd, payload)
        digit = chr(payload[0])
        port = payload[1] if len(payload) > 1 else 0
        return DtmfEvent(port=port, digit=digit)
    if cmd == 0x64:
        return PhysEvent(payload)
    if cmd in (0x40, 0x60):                           # poll reply
        return PollStatus(port=port_of(cmd), status=payload[0] if payload else -1)
    return Unknown(cmd, payload)
