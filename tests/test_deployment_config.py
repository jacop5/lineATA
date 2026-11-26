"""Offline regression tests for the deployment draft (deploy/, docs/).

No hardware, network or service activation: the daemon CLI is only exercised
through argparse (--help / invalid values), which exits before any USB access.
"""
import configparser
import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "deploy" / "lineata.service"
ENV_EXAMPLE = ROOT / "deploy" / "lineata.env.example"
UDEV = ROOT / "deploy" / "99-lineata.rules"


def load_unit():
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    with UNIT.open() as fh:
        parser.read_file(fh)
    return parser


def load_env_example():
    values = {}
    for raw in ENV_EXAMPLE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        assert sep, f"env example line is not KEY=VALUE: {raw!r}"
        values[key.strip()] = value
    return values


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "bridge.ata", *args],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )


def test_start_limit_lives_in_unit_section():
    unit = load_unit()
    assert unit["Unit"]["StartLimitIntervalSec"] == "0"
    assert "StartLimitIntervalSec" not in unit["Service"]
    assert unit["Service"]["Restart"] == "always"
    assert int(unit["Service"]["RestartSec"]) >= 1


def test_service_runs_as_non_root_with_absolute_paths():
    unit = load_unit()
    svc = unit["Service"]
    assert svc["User"] not in ("", "root")
    assert svc["Group"] == svc["User"]
    assert "dialout" in svc["SupplementaryGroups"]
    assert svc["EnvironmentFile"] == "/etc/lineata.env"
    for key in ("WorkingDirectory", "ExecStart"):
        assert svc[key].startswith("/"), f"{key} must be an absolute path"
    assert svc["ExecStart"].startswith("/opt/lineata/.venv/bin/python -m bridge.ata")


def test_every_execstart_variable_is_defined_in_env_example():
    unit = load_unit()
    referenced = set(re.findall(r"\$\{(\w+)\}", unit["Service"]["ExecStart"]))
    referenced.add("LINEATA_SIP_PASSWORD")  # consumed by the daemon from env
    defined = set(load_env_example())
    assert referenced <= defined, referenced - defined


def test_env_example_values_have_no_inline_comments_or_whitespace():
    for raw in ENV_EXAMPLE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        value = line.partition("=")[2]
        assert "#" not in value, f"inline comment would become value text: {raw!r}"
        assert value == value.strip() and value, f"empty/padded value: {raw!r}"


def test_env_example_values_are_valid_ports_ip_and_placeholders():
    env = load_env_example()
    for key in ("LINEATA_PBX_PORT", "LINEATA_SIP_PORT"):
        port = int(env[key])  # raises if not an integer
        assert 1 <= port <= 65535
    ipaddress.IPv4Address(env["LINEATA_LOCAL_IP"])  # raises if not an IPv4
    assert env["LINEATA_PBX"], "PBX host placeholder must be non-empty"
    assert re.fullmatch(r"[A-Za-z0-9.-]+", env["LINEATA_PBX"])
    assert env["LINEATA_LOGIN"] and env["LINEATA_SIP_PASSWORD"]
    # placeholders must be generic documentation values, not real-looking hosts
    assert env["LINEATA_PBX"].endswith(".invalid")
    assert ipaddress.IPv4Address(env["LINEATA_LOCAL_IP"]).is_private


def test_execstart_flags_exist_in_real_cli_help():
    unit = load_unit()
    exec_flags = set(re.findall(r"(--[a-z-]+)", unit["Service"]["ExecStart"]))
    assert exec_flags, "no CLI flags found in ExecStart"
    help_run = cli("--help")
    assert help_run.returncode == 0, help_run.stderr
    for flag in exec_flags:
        assert flag in help_run.stdout, f"{flag} is not a real bridge.ata CLI flag"


def test_cli_port_int_boundary_without_reaching_the_daemon():
    # `--help` short-circuits parsing, so it can NOT prove value acceptance.
    # Instead, probe the real argparse boundary with a guaranteed parse error
    # AFTER the port flag: if "5060" passes type=int, the failure is the later
    # missing --login value ("expected one argument"); if it fails type=int,
    # the error is "invalid int value". argparse SystemExit (rc=2) happens
    # before main()'s body, so no USB device or network access is possible.
    env = {k: v for k, v in os.environ.items() if k != "LINEATA_SIP_PASSWORD"}

    def cli_clean(*args):
        return subprocess.run(
            [sys.executable, "-m", "bridge.ata", *args],
            cwd=ROOT, capture_output=True, text=True, timeout=60, env=env,
        )

    for flag in ("--pbx-port", "--sip-port"):
        ok = cli_clean(flag, "5060", "--login")  # --login missing its value
        assert ok.returncode == 2, ok.stderr
        assert "expected one argument" in ok.stderr, ok.stderr
        assert "invalid int value" not in ok.stderr
        bad = cli_clean(flag, "not-a-port", "--login")
        assert bad.returncode == 2
        assert "invalid int value" in bad.stderr


def test_udev_rules_are_scoped_to_the_dongle_only():
    rules = [l.strip() for l in UDEV.read_text().splitlines()
             if l.strip() and not l.startswith("#")]
    assert rules, "udev rules file has no active rules"
    for rule in rules:
        assert 'idVendor}=="31b7"' in rule
        assert re.search(r'idProduct}=="000[12]"', rule)
        assert 'GROUP="dialout"' in rule and 'MODE="0660"' in rule
    # no broad subsystem-only grants
    assert not any("idVendor" not in r for r in rules)


def test_docs_state_linux_validation_limit():
    doc = (ROOT / "docs" / "deployment.md").read_text()
    assert "not validated on Linux" in doc
    assert "systemd-analyze verify" in doc
    assert "ttyACM" in doc  # generic tty-selection warning present


def test_cli_requires_explicit_pbx_and_local_addresses():
    # No environment secret and no addresses: argparse must exit before USB.
    env = {k: v for k, v in os.environ.items() if k != 'LINEATA_SIP_PASSWORD'}
    run = subprocess.run(
        [sys.executable, '-m', 'bridge.ata', '--login', 'test-only'],
        cwd=ROOT, capture_output=True, text=True, timeout=10, env=env,
    )
    assert run.returncode == 2
    assert '--pbx' in run.stderr and '--local-ip' in run.stderr
    assert 'required' in run.stderr
