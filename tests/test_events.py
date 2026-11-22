import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bridge"))
import events as EV


def test_hook_off():
    # observed wire format: a5 04 <state> <port>  (2026-11-23 capture)
    e = EV.decode(bytes([0xA5, 0x04, 0x01, 0x00]))
    assert isinstance(e, EV.HookEvent) and e.offhook and e.port == 0


def test_hook_on_port1():
    e = EV.decode(bytes([0xA5, 0x04, 0x00, 0x01]))
    assert isinstance(e, EV.HookEvent) and not e.offhook and e.port == 1


def test_dtmf():
    # observed on the wire: a4 04 <digit-ascii> 00 (2026-11-23 capture)
    e = EV.decode(bytes([0xA4, 0x04, 0x30, 0x00]))
    assert isinstance(e, EV.DtmfEvent) and e.digit == "0"
    e = EV.decode(bytes([0xA4, 0x04, 0x35, 0x00]))
    assert isinstance(e, EV.DtmfEvent) and e.digit == "5"


def test_poll_status():
    e = EV.decode(bytes([0x40, 0x03, 0x00]))
    assert isinstance(e, EV.PollStatus) and e.port == 0 and e.status == 0
    e = EV.decode(bytes([0x60, 0x03, 0x8F]))
    assert isinstance(e, EV.PollStatus) and e.port == 1 and e.status == 0x8F


def test_frame_layout():
    """Frames are [cmd][len][payload]; len excludes header."""
    assert EV.port_of(0x40) == 0
    assert EV.port_of(0x60) == 1
    assert EV.port_of(0x4e) == 0
    assert EV.port_of(0x6e) == 1


def test_unknown_passthrough():
    e = EV.decode(bytes([0xAB, 0x02, 0x11, 0x22]))
    assert isinstance(e, EV.Unknown) and e.cmd == 0xAB
