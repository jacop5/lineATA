"""Startup-cancellation regressions (TASK-0031, methodology-corrected).

Planned minimal API under test (default None preserves the current wire/API):
  reset_to_bootloader(timeout=6, should_cancel=None)
  boot_app(timeout=10, should_cancel=None)
  connect() passes a combined stop/request predicate; usb_init checks its own
  stop/request at per-frame/delay/read checkpoints; run() must not start the
  USB thread after a cancel during init and must dispose the failed handle
  without a cold reset.

Methodology (corrected after lead review):
- Rig.request() calls the CURRENT instance's request_shutdown(), so the ACTUAL
  ata.shutdown_requested flag is set (never a TRACE-only boolean); run-based
  cases assert ata.shutdown_requested.is_set().
- Trigger callbacks perform the original fake operation FIRST, then set the
  request, then return; assertions check that NO SUBSEQUENT operation happens.
- The positive control gates PID_LINEAR appearance on the observed 'b' boot
  byte, so the boot byte is genuinely written before the device appears.
- Fake termios exposes the error class; FakeClock and NoThread are robust.

At baseline the should_cancel kwarg does not exist (TypeError = planned missing
feature, not proof by itself); decisive behavioral evidence is TASK-0028's
corrected probe log. No real USB/TTY/socket/thread.
"""
import sys
import threading as real_threading
import time as real_time
import types
from unittest.mock import Mock

import pytest

import bridge.ata as ata_mod
from bridge.ata import LinearATA, PID_LINEAR, PID_CDC, OPS

CFG = {'login': 't', 'password': 'x', 'domain': 'd', 'pbx_ip': '127.0.0.1',
       'pbx_port': 5060, 'local_ip': '127.0.0.1', 'sip_port': 5062, 'cold': True}


class FakeClock:
    def __init__(self, rig):
        self.rig = rig
        self.now = 1000.0
        self.sleeps_after = []

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, s):
        # perform the sleep first; only THEN may a trigger fire, so the
        # triggering sleep is never counted as "after request"
        self.now += s
        self.rig.trace['sleep'] += 1
        if self.rig.requested:
            self.sleeps_after.append(s)
        self.rig.maybe_trigger('sleep')

    def strftime(self, *a):
        return real_time.strftime(*a)


class Rig:
    def __init__(self, monkeypatch):
        self.requested = False
        self.ata = None
        self.trace = dict.fromkeys(
            ['find', 'find_after', 'tty_open', 'tty_open_after', 'tty_write',
             'tty_write_after', 'tty_close', 'tty_close_after', 'dev_write',
             'dev_write_after', 'dev_read', 'dev_read_after', 'resets_after',
             'thread_starts', 'sleep'], 0)
        self.disposed = []
        self.triggers = {}  # kind -> callable(n) -> bool, fire once
        self.dev = self._make_dev()
        self.cdc_dev = Mock()
        self.cdc_dev.idProduct = PID_CDC
        self.boot_byte_seen = False

        self.clock = FakeClock(self)
        monkeypatch.setattr(ata_mod, 'time', self.clock)
        monkeypatch.setattr(ata_mod.usb.core, 'find', self._find)
        monkeypatch.setattr(ata_mod.usb.util, 'claim_interface', lambda *a, **k: None)
        monkeypatch.setattr(ata_mod.usb.util, 'dispose_resources',
                            lambda d: self.disposed.append(d))
        monkeypatch.setattr(ata_mod, '_find_tty', lambda: '/dev/fake0')
        monkeypatch.setattr(ata_mod, 'os', self._os_shim())
        monkeypatch.setitem(sys.modules, 'termios', self._termios_shim())

        rig = self

        class NoThread:
            def __init__(self, *a, **kw):
                pass

            def start(self):
                rig.trace['thread_starts'] += 1

            def join(self, timeout=None):
                pass

            def is_alive(self):
                return False

        class ThreadShim:
            Thread = NoThread

            def __getattr__(self, name):
                return getattr(real_threading, name)

        monkeypatch.setattr(ata_mod, 'threading', ThreadShim())

    # -- actual-flag request --
    def request(self):
        self.requested = True
        if self.ata is not None:
            self.ata.request_shutdown()  # the REAL flag, not a trace boolean

    def maybe_trigger(self, kind):
        fn = self.triggers.get(kind)
        if fn is not None and not self.requested and fn(self.trace[kind]):
            self.request()

    def _make_dev(self):
        rig = self
        dev = Mock()
        dev.idProduct = PID_LINEAR
        dev.get_active_configuration.return_value = {(0, 0): Mock()}
        last = {'cmd': 0}

        def write(ep, data, timeout):
            rig.trace['dev_write'] += 1
            if data:
                last['cmd'] = data[0]
            if rig.requested:
                rig.trace['dev_write_after'] += 1
            rig.maybe_trigger('dev_write')
            return len(data)

        def read(ep, size, timeout):
            rig.trace['dev_read'] += 1
            if rig.requested:
                rig.trace['dev_read_after'] += 1
            rig.maybe_trigger('dev_read')
            return bytes([last['cmd'], 4, 0, 0])  # immediate matching ACK

        def reset():
            if rig.requested:
                rig.trace['resets_after'] += 1

        dev.write = Mock(side_effect=write)
        dev.read = Mock(side_effect=read)
        dev.reset = Mock(side_effect=reset)
        return dev

    def _find(self, **kw):
        self.trace['find'] += 1
        if self.requested:
            self.trace['find_after'] += 1
        rig = self

        def result():
            if 'idProduct' in kw:
                if kw['idProduct'] == PID_CDC:
                    return rig.cdc_dev
                # PID_LINEAR appears only after the boot byte was observed
                if rig.boot_byte_seen:
                    return rig.dev
                return None
            return rig.cdc_dev

        out = result()
        self.maybe_trigger('find')
        return out

    def _os_shim(self):
        import os as real_os
        rig = self

        class OsShim:
            O_RDWR = real_os.O_RDWR
            O_NOCTTY = real_os.O_NOCTTY
            O_NONBLOCK = real_os.O_NONBLOCK

            def open(self, *a):
                rig.trace['tty_open'] += 1
                if rig.requested:
                    rig.trace['tty_open_after'] += 1
                rig.maybe_trigger('tty_open')
                return 99

            def write(self, fd, data):
                rig.trace['tty_write'] += 1
                assert data == b'b', 'boot wire byte changed'
                rig.boot_byte_seen = True
                if rig.requested:
                    rig.trace['tty_write_after'] += 1
                return 1

            def close(self, fd):
                rig.trace['tty_close'] += 1
                if rig.requested:
                    rig.trace['tty_close_after'] += 1

        return OsShim()

    def _termios_shim(self):
        fake = types.ModuleType('termios')
        fake.TCSANOW = 0
        fake.error = type('error', (Exception,), {})  # error class exposed
        fake.tcgetattr = Mock(return_value=[0] * 7)
        fake.tcsetattr = Mock()
        return fake

    def make_ata(self):
        self.ata = LinearATA(dict(CFG))
        self.ata.log = lambda *a: None
        return self.ata


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


# ---- preservation controls (expected to pass at baseline) ----

def test_positive_boot_byte_then_full_mock_init_preserved(rig):
    ata = rig.make_ata()
    assert ata.run() is True
    assert rig.trace['tty_write'] == 1          # exactly one 'b' boot byte
    assert rig.boot_byte_seen                       # and it gated device appearance
    assert rig.trace['dev_write'] == len(OPS)    # full init replay
    assert rig.trace['thread_starts'] == 1       # USB loop started once


def test_pre_set_request_prevents_any_discovery(rig):
    ata = rig.make_ata()
    ata.shutdown_requested.set()
    assert ata.run() is False
    assert ata.shutdown_requested.is_set()
    assert rig.trace['find'] == 0 and rig.trace['tty_write'] == 0
    assert rig.trace['thread_starts'] == 0


# ---- planned API kwargs (expected TypeError at baseline) ----

def test_reset_to_bootloader_should_cancel_stops_polling(rig):
    ata = rig.make_ata()
    # Keep service mode visible so the reset helper really enters its poll.
    rig.cdc_dev.idProduct = PID_LINEAR
    rig.triggers['find'] = lambda n: n >= 3  # cancel mid-poll, after the find
    result = ata_mod.reset_to_bootloader(timeout=6, should_cancel=lambda: rig.requested)
    assert result is False
    assert ata.shutdown_requested.is_set()
    assert rig.trace['find_after'] == 0
    assert rig.clock.sleeps_after == []


def test_boot_app_should_cancel_prevents_boot_byte(rig):
    rig.make_ata()
    rig.triggers['find'] = lambda n: n >= 2  # cancel before the boot write
    result = ata_mod.boot_app(timeout=10, should_cancel=lambda: rig.requested)
    assert result is False
    assert rig.trace['tty_write_after'] == 0


# ---- run-based cancellation checkpoints (fail at baseline until implemented) ----

def test_mid_find_cancel_stops_connect_polling(rig):
    ata = rig.make_ata()
    rig.triggers['find'] = lambda n: n >= 5
    assert ata.run() is False
    assert ata.shutdown_requested.is_set()      # ACTUAL flag, not a trace bool
    assert rig.trace['find_after'] == 0         # no discovery after cancel
    assert rig.clock.sleeps_after == []
    assert rig.trace['tty_write_after'] == 0
    assert rig.trace['thread_starts'] == 0


def test_mid_tty_open_cancel_skips_boot_byte_and_closes_fd(rig):
    ata = rig.make_ata()
    rig.triggers['tty_open'] = lambda n: True    # cancel right after open
    assert ata.run() is False
    assert ata.shutdown_requested.is_set()
    assert rig.trace['tty_write_after'] == 0    # no 'b' after observed cancel
    assert rig.trace['tty_close'] >= 1           # owned fd closed
    assert rig.trace['thread_starts'] == 0


def test_mid_init_write_cancel_stops_frames_and_thread(rig):
    ata = rig.make_ata()
    rig.triggers['dev_write'] = lambda n: n >= 50
    assert ata.run() is False
    assert ata.shutdown_requested.is_set()
    assert rig.trace['dev_write_after'] == 0    # no init frames after cancel
    assert rig.trace['dev_read_after'] == 0
    assert rig.trace['thread_starts'] == 0      # USB loop never started
    assert rig.dev in rig.disposed               # failed handle disposed
    assert rig.trace['resets_after'] == 0        # no cold reset after cancel


def test_mid_init_read_cancel_stops_subsequent_ops(rig):
    ata = rig.make_ata()
    rig.triggers['dev_read'] = lambda n: n >= 10
    assert ata.run() is False
    assert ata.shutdown_requested.is_set()
    assert rig.trace['dev_read_after'] == 0
    assert rig.trace['dev_write_after'] == 0
    assert rig.trace['thread_starts'] == 0


def test_mid_init_delay_cancel_stops_subsequent_ops(rig):
    ata = rig.make_ata()
    rig.triggers['sleep'] = lambda n: n >= 5     # cancel during an init delay
    assert ata.run() is False
    assert ata.shutdown_requested.is_set()
    assert rig.clock.sleeps_after == []          # no sleeps after cancel
    assert rig.trace['dev_write_after'] == 0
    assert rig.trace['thread_starts'] == 0
