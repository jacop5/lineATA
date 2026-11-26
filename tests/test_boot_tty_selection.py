"""Daemon integration for identity-checked boot tty discovery; no real devices."""
from unittest.mock import Mock

from bridge import ata


def test_daemon_uses_identity_discovery(monkeypatch):
    discovery = Mock(return_value='/dev/ttyACM7')
    monkeypatch.setattr(ata, 'find_boot_tty', discovery)
    assert ata._find_tty() == '/dev/ttyACM7'
    discovery.assert_called_once_with()


def test_no_identified_boot_tty_never_opens_or_writes(monkeypatch):
    monkeypatch.setattr(ata.usb.core, 'find', Mock(return_value=None))
    monkeypatch.setattr(ata, 'find_boot_tty', Mock(return_value=None))
    open_tty = Mock(side_effect=AssertionError('must not open an unidentified tty'))
    monkeypatch.setattr(ata.os, 'open', open_tty)
    assert ata.boot_app() is False
    open_tty.assert_not_called()
