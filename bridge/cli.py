"""Pre-start CLI value validators for bridge.ata.

Stdlib only; no import-time side effects. Each helper is an argparse `type=`
callable: it either returns the typed value or raises ArgumentTypeError, so
invalid input fails during parse_args — before any constructor, USB or network
side effect. Non-integer ports keep the builtin-like "invalid int value"
message so existing CLI regression semantics hold.
"""
import argparse
import ipaddress
import math


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from None


def pbx_port(value):
    """PBX SIP port: 1..65535."""
    port = _as_int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(
            f"--pbx-port must be in 1..65535, got {port}")
    return port


def sip_port(value):
    """Local SIP listen port: 0..65535; 0 = ephemeral (internal probe compatibility)."""
    port = _as_int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(
            f"--sip-port must be in 0..65535 (0 selects an ephemeral port), got {port}")
    return port


def local_ip(value):
    """Local IPv4 literal: reject unspecified/multicast/limited-broadcast;
    allow loopback (test addresses). No interface or DNS queries, no claim that
    the address is assigned."""
    try:
        addr = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise argparse.ArgumentTypeError(
            f"--local-ip must be an IPv4 literal, got {value!r}") from None
    limited_broadcast = ipaddress.IPv4Address("255.255.255.255")
    if (addr.is_unspecified or addr.is_multicast
            or addr == limited_broadcast):
        raise argparse.ArgumentTypeError(
            f"--local-ip must be a usable unicast IPv4 address, got {value!r}")
    return value


def dial_timeout(value):
    """Dial timeout in seconds: finite and strictly positive, no upper bound."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"invalid float value: {value!r}") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError(
            f"--dial-timeout must be a finite positive number of seconds, got {value!r}")
    return seconds
