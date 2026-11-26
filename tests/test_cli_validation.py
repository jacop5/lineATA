"""CLI boundary tests (TASK-0027 audit, TASK-0029 desired behavior).

Baseline note: the earlier 36 green tests encoded KNOWN-invalid parser
acceptance (out-of-range ports, unusable --local-ip values, non-finite
--dial-timeout reaching the constructor). Those observational pins are now
converted into desired regressions: invalid input => argparse exit 2 with the
constructor/run sentinels at 0; valid input => the sentinel stub is reached
with a typed cfg and zero I/O. --help is only asserted as an early exit, never
as value-acceptance proof.

Method: replace bridge.ata.LinearATA with a recording sentinel (run/start_sip
stubbed; start_sip requests shutdown so main's wait loop exits immediately).
No USB, no real sockets, no network.
"""
import sys
import threading

import pytest

import bridge.ata as ata_mod
from bridge.ata import ShutdownRequest

BASE = ["prog", "--pbx", "pbx.example.invalid", "--login", "x",
        "--password", "test-only", "--local-ip", "127.0.0.1"]


class SentinelATA:
    constructed = 0
    run_calls = 0
    start_sip_calls = 0
    last = None

    def __init__(self, cfg):
        type(self).constructed += 1
        type(self).last = self
        self.cfg = cfg
        self.shutdown_requested = ShutdownRequest()
        self.stop = threading.Event()
        self.debug_frames = False

    def log(self, *a):
        pass

    def run(self):
        type(self).run_calls += 1
        return True

    def start_sip(self, register=True):
        type(self).start_sip_calls += 1
        self.shutdown_requested.set()  # make main's wait loop exit at once

    def shutdown(self, timeout=10.0):
        return "ok"


@pytest.fixture
def sentinel(monkeypatch):
    SentinelATA.constructed = SentinelATA.run_calls = SentinelATA.start_sip_calls = 0
    SentinelATA.last = None
    monkeypatch.setattr(ata_mod, "LinearATA", SentinelATA)
    monkeypatch.delenv("LINEATA_SIP_PASSWORD", raising=False)
    return SentinelATA


def run_main(monkeypatch, argv):
    """Run the real main(); argparse errors raise SystemExit(2); a normal
    requested-shutdown return from main() counts as exit code 0."""
    monkeypatch.setattr(sys, "argv", argv)
    try:
        ata_mod.main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    return 0


def reaches_stub(sentinel, argv, monkeypatch, expect_code=0):
    code = run_main(monkeypatch, argv)
    assert code == expect_code, (code, argv)
    return sentinel


def rejected_before_constructor(sentinel, argv, monkeypatch, capsys=None, stderr_needle=None):
    code = run_main(monkeypatch, argv)
    assert code == 2, (code, argv)
    assert sentinel.constructed == 0 and sentinel.run_calls == 0
    if stderr_needle is not None:
        assert stderr_needle in capsys.readouterr().err


# ---- --pbx-port: 1..65535 valid; everything else rejected pre-constructor ----
@pytest.mark.parametrize("port", ["1", "5060", "65535"])
def test_pbx_port_valid_range_reaches_stub(sentinel, monkeypatch, port):
    reaches_stub(sentinel, BASE + ["--pbx-port", port], monkeypatch)
    assert sentinel.constructed == 1 and sentinel.run_calls == 1 and sentinel.start_sip_calls == 1
    assert sentinel.last.cfg["pbx_port"] == int(port)


@pytest.mark.parametrize("port", ["0", "-1", "65536", "99999999999"])
def test_pbx_port_out_of_range_rejected_before_constructor(sentinel, monkeypatch, port):
    rejected_before_constructor(sentinel, BASE + ["--pbx-port", port], monkeypatch)


def test_pbx_port_nonint_keeps_builtin_like_message(sentinel, monkeypatch, capsys):
    rejected_before_constructor(sentinel, BASE + ["--pbx-port", "abc"], monkeypatch,
                                capsys=capsys, stderr_needle="invalid int value")


# ---- --sip-port: 0..65535 valid (0 = ephemeral, preserved) ----
@pytest.mark.parametrize("port", ["0", "1", "5062", "65535"])
def test_sip_port_zero_and_valid_range_reach_stub(sentinel, monkeypatch, port):
    reaches_stub(sentinel, BASE + ["--sip-port", port], monkeypatch)
    assert sentinel.last.cfg["sip_port"] == int(port)  # 0 preserved for internal probes


@pytest.mark.parametrize("port", ["-1", "65536"])
def test_sip_port_out_of_range_rejected_before_constructor(sentinel, monkeypatch, port):
    rejected_before_constructor(sentinel, BASE + ["--sip-port", port], monkeypatch)


def test_sip_port_nonint_rejected_before_constructor(sentinel, monkeypatch):
    rejected_before_constructor(sentinel, BASE + ["--sip-port", "xyz"], monkeypatch)


# ---- --local-ip: IPv4 literal unicast only; loopback allowed ----
@pytest.mark.parametrize("ip", ["127.0.0.1", "192.0.2.10", "240.0.0.1"])
def test_local_ip_ipv4_literals_reach_stub(sentinel, monkeypatch, ip):
    reaches_stub(sentinel, BASE[:-1] + [ip], monkeypatch)
    assert sentinel.last.cfg["local_ip"] == ip


@pytest.mark.parametrize("ip", ["0.0.0.0", "255.255.255.255", "224.0.0.1",
                                "example.com", "not-an-ip", "::1"])
def test_local_ip_non_usable_rejected_before_constructor(sentinel, monkeypatch, ip):
    rejected_before_constructor(sentinel, BASE[:-1] + [ip], monkeypatch)


# ---- --pbx stays a plain hostname string: allowed, no DNS resolution in the parser ----
def test_pbx_hostname_allowed_without_dns(sentinel, monkeypatch):
    reaches_stub(sentinel, BASE, monkeypatch)
    assert sentinel.last.cfg["pbx_ip"] == "pbx.example.invalid"


# ---- --dial-timeout: finite positive only ----
@pytest.mark.parametrize("v", ["0.1", "3.0", "60", "1e9"])
def test_dial_timeout_positive_finite_reaches_stub(sentinel, monkeypatch, v):
    reaches_stub(sentinel, BASE + ["--dial-timeout=" + v], monkeypatch)
    assert sentinel.last.cfg["dial_timeout"] == float(v)


@pytest.mark.parametrize("v", ["0", "-1", "nan", "inf", "-inf"])
def test_dial_timeout_nonpositive_or_nonfinite_rejected(sentinel, monkeypatch, v):
    rejected_before_constructor(sentinel, BASE + ["--dial-timeout=" + v], monkeypatch)


def test_dial_timeout_nonfloat_rejected_before_constructor(sentinel, monkeypatch, capsys):
    rejected_before_constructor(sentinel, BASE + ["--dial-timeout=soon"], monkeypatch,
                                capsys=capsys, stderr_needle="invalid float value")


# ---- --help stays an early exit; never used with values as acceptance proof ----
def test_help_exits_zero_before_constructor(sentinel, monkeypatch, capsys):
    code = run_main(monkeypatch, ["prog", "--help"])
    assert code == 0
    assert "usage" in capsys.readouterr().out.lower()
    assert sentinel.constructed == 0


# ---- auth: --password flag and LINEATA_SIP_PASSWORD env preserved ----
def test_password_from_env_reaches_stub(sentinel, monkeypatch):
    monkeypatch.setenv("LINEATA_SIP_PASSWORD", "test-only-env")
    argv = ["prog", "--pbx", "pbx.example.invalid", "--login", "x",
            "--local-ip", "127.0.0.1"]
    reaches_stub(sentinel, argv, monkeypatch)
    assert sentinel.last.cfg["password"] == "test-only-env"


def test_missing_password_rejected_with_auth_specific_error(sentinel, monkeypatch, capsys):
    # Remove ONLY the --password flag and its value; all required flags stay.
    argv = ["prog", "--pbx", "pbx.example.invalid", "--login", "x",
            "--local-ip", "127.0.0.1"]
    rejected_before_constructor(sentinel, argv, monkeypatch,
                                capsys=capsys, stderr_needle="LINEATA_SIP_PASSWORD")


def test_password_flag_takes_precedence_over_env(sentinel, monkeypatch):
    monkeypatch.setenv("LINEATA_SIP_PASSWORD", "test-only-env")
    reaches_stub(sentinel, BASE, monkeypatch)  # BASE carries --password test-only
    assert sentinel.last.cfg["password"] == "test-only"
