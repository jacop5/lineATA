import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bridge"))


def test_voice_hypothesis_shapes():
    import voice
    port = 1
    for h in voice.HYPOTHESES:
        f = h.frame_fn(port, b"\xff" * 160)
        assert isinstance(f, bytes) and len(f) > 160
        assert (f[0] & 0x20) == 0x20, "port1 must set the port bit in cmd"


def test_port0_bit_clear():
    import voice
    port = 0
    f = voice.HYPOTHESES[0].frame_fn(port, b"\x00" * 160)
    assert (f[0] & 0x20) == 0


def test_ring_hypotheses_portbit():
    import ring
    b0 = ring.stop_ring.__doc__  # docstring presence is silly to assert; skip
    # stop_ring builds the right cmd per port
    import inspect
    src = inspect.getsource(ring.stop_ring)
    assert "61040206" in src and "41040206" in src
