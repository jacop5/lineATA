"""Offline USB lifecycle fault-injection tests (mocked; no hardware, no network).

Derived from the saved USB audit
(.orchestration/migration-backups/legacy-context/reports/lineata-usb-reliability-review.md,
defects D2/D3/D5/D7). Tests assert intended *public* behavior of
LinearATA.connect/boot_app/run/usb_loop with mocked usb/os/time; they do not
assert source text, lockfiles, or any speculative recovery design. A mocked
pass never proves real hardware recovery.
"""
import os
import termios
import threading
from unittest.mock import Mock, call

import pytest
import usb.core

from bridge.ata import LinearATA, boot_app

CFG = {'login': 'test', 'password': 'test-only', 'domain': 'example.invalid',
       'pbx_ip': '127.0.0.1', 'pbx_port': 5060, 'local_ip': '127.0.0.1',
       'sip_port': 5062}


class _Delegate:
    """Namespace shim: override selected attributes, delegate the rest."""

    def __init__(self, target, **overrides):
        object.__setattr__(self, '_target', target)
        object.__setattr__(self, '_overrides', overrides)

    def __getattr__(self, name):
        overrides = object.__getattribute__(self, '_overrides')
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, '_target'), name)


def make_dev():
    dev = Mock()
    dev.get_active_configuration.return_value = {(0, 0): Mock()}
    return dev


def test_claim_failure_fails_connection(monkeypatch):
    """D3: a swallowed claim_interface failure must fail connect(), not proceed."""
    dev = make_dev()
    monkeypatch.setattr('bridge.ata.usb.core.find', Mock(return_value=dev))
    monkeypatch.setattr('bridge.ata.usb.util.claim_interface',
                        Mock(side_effect=usb.core.USBError('EBUSY', errno=16)))
    ata = LinearATA(dict(CFG))
    assert ata.connect() is False
    assert ata.dev is None


def test_boot_app_tty_open_oserror_is_retryable(monkeypatch):
    """D5: tty open OSError must yield retryable False, not an escaping error."""
    monkeypatch.setattr('bridge.ata.usb.core.find', Mock(return_value=None))
    monkeypatch.setattr('bridge.ata._find_tty', lambda: '/dev/ttyACM0')
    monkeypatch.setattr('bridge.ata.os', _Delegate(
        os, open=Mock(side_effect=OSError(9, 'Bad file descriptor'))))
    assert boot_app() is False


def test_boot_app_tty_write_oserror_closes_fd_and_is_retryable(monkeypatch):
    """D5: tty write OSError must close the fd and yield retryable False."""
    monkeypatch.setattr('bridge.ata.usb.core.find', Mock(return_value=None))
    monkeypatch.setattr('bridge.ata._find_tty', lambda: '/dev/ttyACM0')
    monkeypatch.setattr(termios, 'tcgetattr', Mock(return_value=[0] * 7))
    monkeypatch.setattr(termios, 'tcsetattr', Mock())
    close = Mock()
    monkeypatch.setattr('bridge.ata.os', _Delegate(
        os, open=Mock(return_value=99),
        write=Mock(side_effect=OSError(5, 'Input/output error')),
        close=close))
    assert boot_app() is False
    close.assert_called_once_with(99)


def test_failed_init_disposes_device_resources(monkeypatch):
    """D5: when usb_init fails, run() must dispose device resources before
    dropping the device, and return False after its bounded retries."""
    monkeypatch.setattr('bridge.ata.time', _Delegate(__import__('time'), sleep=lambda *a: None))
    dispose = Mock()
    monkeypatch.setattr('bridge.ata.usb.util.dispose_resources', dispose)
    ata = LinearATA(dict(CFG))
    devices = [make_dev() for _ in range(5)]
    pending = iter(devices)

    def connect():
        ata.dev = next(pending)
        return True

    monkeypatch.setattr(ata, 'connect', connect)
    monkeypatch.setattr(ata, 'usb_init', Mock(return_value=False))
    assert ata.run() is False
    assert ata.dev is None
    assert dispose.call_args_list == [call(dev) for dev in devices]


def _faulted_ata():
    ata = LinearATA(dict(CFG))
    ata.dev = make_dev()
    ata.dev.read.side_effect = usb.core.USBError('disconnected', errno=19)
    ata.usb_active = True
    return ata


def test_runtime_fault_resets_usb_active_and_queue(monkeypatch):
    """D2: a fatal transport error must not leave usb_active/queued state stale."""
    dispose = Mock()
    monkeypatch.setattr('bridge.ata.usb.util.dispose_resources', dispose)
    ata = _faulted_ata()
    ata.usb_commands.submit(bytes.fromhex('a00300'), 500)  # valid queued control frame
    ata.usb_loop()  # runs in this thread, so drain_commands owner check passes
    assert ata.stop.is_set()          # current behavior (passes)
    assert dispose.called             # current behavior (passes)
    assert ata.usb_active is False    # expected; currently stale True -> failure
    assert ata.usb_commands.pending is None
    assert ata.usb_commands.commands.empty()


def test_runtime_fault_closes_active_dialog(monkeypatch):
    """D7: on runtime USB loss the active dialog must be explicitly closed."""
    monkeypatch.setattr('bridge.ata.usb.util.dispose_resources', Mock())
    ata = _faulted_ata()
    call = Mock()
    call.media = True
    call.media_enable_due = 0  # keep media_tick inert; the fault comes from read()
    ata.call = call
    ata.usb_loop()
    assert ata.stop.is_set()
    call.hangup.assert_called_once_with()  # expected; currently never called -> failure


# ---------------------------------------------------------------- TASK-0012 additions
def test_connect_success_claims_and_keeps_device(monkeypatch):
    """Positive path: claim succeeds -> connect() True, dev set."""
    dev = make_dev()
    monkeypatch.setattr('bridge.ata.usb.core.find', Mock(return_value=dev))
    claim = Mock()
    monkeypatch.setattr('bridge.ata.usb.util.claim_interface', claim)
    ata = LinearATA(dict(CFG))
    assert ata.connect() is True
    assert ata.dev is dev
    claim.assert_called_once()


def test_claim_failure_disposes_device(monkeypatch):
    """Acceptance: claim failure returns False with resources disposed, dev None."""
    dev = make_dev()
    monkeypatch.setattr('bridge.ata.usb.core.find', Mock(return_value=dev))
    monkeypatch.setattr('bridge.ata.usb.util.claim_interface',
                        Mock(side_effect=usb.core.USBError('EBUSY', errno=16)))
    dispose = Mock()
    monkeypatch.setattr('bridge.ata.usb.util.dispose_resources', dispose)
    ata = LinearATA(dict(CFG))
    assert ata.connect() is False
    assert ata.dev is None
    dispose.assert_called_once_with(dev)


def test_boot_app_returns_true_when_already_in_service_mode(monkeypatch):
    monkeypatch.setattr('bridge.ata.usb.core.find',
                        Mock(return_value=Mock(idProduct=0x0001)))
    assert boot_app() is True


def test_failed_init_disposes_on_every_attempt(monkeypatch):
    """run() with failing init must dispose before each retry, no handle leak."""
    monkeypatch.setattr('bridge.ata.time', _Delegate(__import__('time'), sleep=lambda *a: None))
    dispose = Mock()
    monkeypatch.setattr('bridge.ata.usb.util.dispose_resources', dispose)
    ata = LinearATA(dict(CFG))
    devices = [make_dev() for _ in range(5)]
    pending = iter(devices)

    def connect():
        ata.dev = next(pending)
        return True

    monkeypatch.setattr(ata, 'connect', connect)
    monkeypatch.setattr(ata, 'usb_init', Mock(return_value=False))
    assert ata.run() is False
    assert ata.dev is None
    assert dispose.call_args_list == [call(dev) for dev in devices]


def test_run_success_marks_active_and_keeps_device(monkeypatch):
    """Baseline positive path: successful init -> run() True, usb_active, dev kept."""
    dev = make_dev()
    monkeypatch.setattr('bridge.ata.usb.core.find', Mock(return_value=dev))
    ata = LinearATA(dict(CFG))
    monkeypatch.setattr(ata, 'usb_init', Mock(return_value=True))
    monkeypatch.setattr(ata, 'usb_loop', Mock())  # thread target; no real loop
    assert ata.run() is True
    assert ata.usb_active is True
    assert ata.dev is dev


def test_write_guards_prevent_writes_after_fault_and_stop(monkeypatch):
    """usb_active False + dev None/disposed: write must not touch any device,
    and after stop no command is queued or written at all."""
    monkeypatch.setattr('bridge.ata.usb.util.dispose_resources', Mock())
    ata = _faulted_ata()
    ata.usb_loop()  # fault -> cleanup: usb_active False, dev None, stop set
    dev = ata.dev
    assert dev is None and ata.usb_active is False and ata.stop.is_set()
    ata.write('a00300')                      # must be a silent no-op
    assert ata.usb_commands.commands.empty()
    # even with a dev object still around, stop suppresses the write:
    ata2 = LinearATA(dict(CFG))
    ata2.dev = make_dev()
    ata2.usb_active = False
    ata2.stop.set()
    ata2.write('a00300')
    ata2.dev.write.assert_not_called()
    assert ata2.usb_commands.commands.empty()


def test_write_paths_active_queues_and_inactive_direct(monkeypatch):
    """Baseline routing: active -> ControlQueue submit; inactive+dev -> direct."""
    ata = LinearATA(dict(CFG))
    ata.dev = make_dev()
    ata.usb_active = True
    ata.write('a00301', 500)
    assert not ata.usb_commands.commands.empty()
    ata.usb_active = False
    ata.write('a00300', 500)
    ata.dev.write.assert_called_once()
    # submit race vs cleanup: active but stop set -> nothing queued
    ata.usb_active = True
    ata.stop.set()
    ata.write('a00301', 500)
    assert ata.usb_commands.commands.qsize() == 1  # only the earlier frame


def test_runtime_fault_clears_audio_ring_dial_state(monkeypatch):
    dispose = Mock()
    monkeypatch.setattr('bridge.ata.usb.util.dispose_resources', dispose)
    ata = _faulted_ata()
    dev = ata.dev
    ata.push_usb_audio(b'\x00' * 160)
    ata.queue_rtp_audio(b'\x00' * 160)
    ata.ringing = True
    ata.dialing = '69'
    ata.ring_deadline = 1.0
    call = Mock()
    call.media = True
    call.media_enable_due = 0
    ata.call = call
    ata.usb_loop()
    assert ata.stop.is_set()
    assert ata.usb_active is False
    assert ata.dev is None
    assert len(ata.in_audio) == 0 and len(ata.out_audio) == 0
    assert ata.ringing is False and ata.dialing == '' and ata.ring_deadline == 0.0
    assert ata.call is None
    call.hangup.assert_called_once_with()
    dispose.assert_called_once_with(dev)


def test_usb_loop_cleanup_is_idempotent(monkeypatch):
    """Second usb_loop entry after a fault must not re-dispose or re-hangup."""
    dispose = Mock()
    monkeypatch.setattr('bridge.ata.usb.util.dispose_resources', dispose)
    ata = _faulted_ata()
    call = Mock()
    call.media = True
    call.media_enable_due = 0
    ata.call = call
    ata.usb_loop()
    ata.usb_loop()  # stop already set: loop body exits, release is a no-op
    assert dispose.call_count == 1
    call.hangup.assert_called_once_with()


def test_queue_overflow_from_non_owner_signals_stop_and_owner_cleans_up(monkeypatch):
    """A control-queue overflow raised in a non-owner (e.g. SIP) thread must
    signal daemon-wide stop, not silently kill only that thread; the USB owner
    then exits promptly and releases the device."""
    dispose = Mock()
    monkeypatch.setattr('bridge.ata.usb.util.dispose_resources', dispose)
    ata = LinearATA(dict(CFG))
    ata.dev = make_dev()
    ata.usb_active = True
    for _ in range(256):             # fill the queue (maxsize=256) via write()
        ata.write('a00301', 500)     # valid control frame
    assert ata.usb_commands.commands.qsize() == 256
    assert not ata.stop.is_set()
    ata.write('a00301', 500)         # overflow: must not raise, must set stop
    assert ata.stop.is_set()
    assert ata.usb_commands.commands.qsize() == 256  # nothing more queued
    ata.usb_loop()                   # owner sees stop, exits, cleans up
    assert ata.usb_active is False
    assert ata.dev is None
    dispose.assert_called_once()


def test_successful_init_uses_direct_writes(monkeypatch):
    """Baseline preserved: before usb_active, write() goes straight to dev."""
    ata = LinearATA(dict(CFG))
    ata.dev = make_dev()
    ata.usb_active = False
    ata.write('a00300', 500)
    ata.dev.write.assert_called_once_with(0x02, bytes.fromhex('a00300'), 500)
    assert ata.usb_commands.commands.empty()


# ---------------------------------------------------------------- lead rework
def test_boot_app_tcgetattr_termios_error_is_retryable(monkeypatch):
    """termios.error is NOT an OSError on this Python: tcgetattr failing must
    return retryable False and close the fd."""
    monkeypatch.setattr('bridge.ata.usb.core.find', Mock(return_value=None))
    monkeypatch.setattr('bridge.ata._find_tty', lambda: '/dev/ttyACM0')
    close = Mock()
    monkeypatch.setattr('bridge.ata.os', _Delegate(
        os, open=Mock(return_value=99), close=close))
    monkeypatch.setattr(termios, 'tcgetattr', Mock(side_effect=termios.error('not a tty')))
    monkeypatch.setattr(termios, 'tcsetattr', Mock())
    assert boot_app() is False
    close.assert_called_once_with(99)


def test_boot_app_tcsetattr_termios_error_is_retryable(monkeypatch):
    """tcsetattr failing with termios.error must return False, fd closed."""
    monkeypatch.setattr('bridge.ata.usb.core.find', Mock(return_value=None))
    monkeypatch.setattr('bridge.ata._find_tty', lambda: '/dev/ttyACM0')
    close = Mock()
    monkeypatch.setattr('bridge.ata.os', _Delegate(
        os, open=Mock(return_value=99), write=Mock(), close=close))
    monkeypatch.setattr(termios, 'tcgetattr', Mock(return_value=[0] * 7))
    monkeypatch.setattr(termios, 'tcsetattr',
                        Mock(side_effect=termios.error('bad attrs')))
    assert boot_app() is False
    close.assert_called_once_with(99)


def test_connect_failure_does_not_retain_preexisting_dev(monkeypatch):
    """Cleanup precondition: connect() returning False must not leave a stale
    self.dev from an earlier connection."""
    dev = make_dev()
    stale = make_dev()
    monkeypatch.setattr('bridge.ata.usb.core.find', Mock(return_value=dev))
    monkeypatch.setattr('bridge.ata.usb.util.claim_interface',
                        Mock(side_effect=usb.core.USBError('EBUSY', errno=16)))
    dispose = Mock()
    monkeypatch.setattr('bridge.ata.usb.util.dispose_resources', dispose)
    ata = LinearATA(dict(CFG))
    ata.dev = stale  # pre-existing device handle
    assert ata.connect() is False
    assert ata.dev is None          # stale dev not retained on failure
    assert dispose.call_args_list == [call(stale), call(dev)]


def test_submit_vs_cleanup_is_serialized_by_state_lock(monkeypatch):
    """Pause a producer inside submit while holding state lock; cleanup must
    run after that submission and leave no command behind. No timing sleeps."""
    ata = _faulted_ata()
    entered = threading.Event()
    proceed = threading.Event()
    original_submit = ata.usb_commands.submit
    errors = []

    def submit(*args, **kwargs):
        entered.set()
        if not proceed.wait(2):
            raise AssertionError('producer barrier timed out')
        original_submit(*args, **kwargs)

    def produce():
        try:
            ata.write('a00301', 500)
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(ata.usb_commands, 'submit', submit)
    producer = threading.Thread(target=produce)
    producer.start()
    try:
        assert entered.wait(2)
        assert ata.usb_state_lock.locked()
        ata.stop.set()
        cleanup = threading.Thread(target=ata._usb_fault_cleanup)
        cleanup.start()
        proceed.set()
        producer.join(2)
        cleanup.join(2)
        assert not producer.is_alive() and not cleanup.is_alive()
        assert not errors
        assert ata.usb_commands.pending is None
        assert ata.usb_commands.commands.empty()
        ata.write('a00301', 500)
        assert ata.usb_commands.commands.empty()
    finally:
        proceed.set()
        producer.join(2)


def test_queue_overflow_exit_clears_pending_queue_and_closes_dialog(monkeypatch):
    dispose = Mock()
    monkeypatch.setattr('bridge.ata.usb.util.dispose_resources', dispose)
    ata = _faulted_ata()
    call = Mock(media=False, media_enable_due=0)
    ata.call = call
    for _ in range(257):
        ata.write('a00301', 500)
    assert ata.stop.is_set()
    ata.usb_loop()  # stop is already set: normal loop exit, no read exception
    assert ata.usb_commands.pending is None
    assert ata.usb_commands.commands.empty()
    assert ata.call is None
    call.hangup.assert_called_once_with()
    call.close.assert_called_once_with()
    dispose.assert_called_once()


def test_fatal_cleanup_closes_call_resources_even_if_hangup_fails(monkeypatch):
    monkeypatch.setattr('bridge.ata.usb.util.dispose_resources', Mock())
    ata = _faulted_ata()
    call = Mock(media=False, media_enable_due=0)
    call.hangup.side_effect = OSError('SIP unavailable')
    ata.call = call
    ata.usb_loop()
    call.close.assert_called_once_with()
    assert ata.call is None
