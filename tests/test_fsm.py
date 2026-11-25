"""Legacy FSM and guessed init boundary checks; not current daemon validation."""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bridge"))


def test_portfsm_outgoing_flow():
    import sipside
    got = []
    fsm = sipside.PortFSM(0, lambda p, n: got.append((p, n)), dial_timeout=0.05)
    assert fsm.state == sipside.PortCallState.IDLE
    fsm.on_hook(True)
    assert fsm.state == sipside.PortCallState.DIALING
    for d in "600":
        ev = fsm.on_dtmf(d)
        assert ev == ("collecting", fsm.digits)
    time.sleep(0.12)          # fire timer
    assert got == [(0, "600")], got
    assert fsm.state == sipside.PortCallState.ACTIVE
    fsm.on_hook(False)
    assert fsm.state == sipside.PortCallState.IDLE


def test_portfsm_hangup_active():
    import sipside
    fsm = sipside.PortFSM(1, lambda p, n: None, dial_timeout=0.05)
    fsm.state = sipside.PortCallState.ACTIVE
    r = fsm.on_hook(False)
    assert r == ("hangup", None)
    assert fsm.state == sipside.PortCallState.IDLE


def test_portfsm_dtmf_ignored_when_active():
    import sipside
    fsm = sipside.PortFSM(0, lambda p, n: None, dial_timeout=0.05)
    fsm.state = sipside.PortCallState.ACTIVE
    assert fsm.on_dtmf("1") is None


def test_initsequence_shape():
    import initsequence
    assert len(initsequence.OPS) > 200
    from initctl import INIT_OPS
    assert len(INIT_OPS) == 84, "clean init boundary must be op 84"
    assert INIT_OPS[-1][1].startswith("65081a04"), "init must end at RAM-mute marker"
    starts = initsequence.OPS[0]
    assert starts[1].startswith("81")   # nvox begins with per-port start/stop
    for delay, h in initsequence.OPS:
        assert 0 <= delay <= 3000
        b = bytes.fromhex(h)
        # len byte semantics vary per command family (payload vs total):
        # c6/c2 use payload length, some init ops count header bytes.
        assert len(b) >= 2 and 0 <= b[1] <= 0xA2
        assert b[0] != 0
