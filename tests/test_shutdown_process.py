"""Deliver actual OS signals to main() with device/SIP startup replaced.

The child imports the real daemon but never opens USB or a network socket.
This checks signal wiring, rather than directly calling a proposed handler.
"""
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
CHILD = r'''
import os
import sys
from types import SimpleNamespace
import bridge.ata as module

class SilentATA(module.LinearATA):
    def run(self):
        if os.environ.get('LINEATA_TEST_SIGNAL_PHASE') == 'startup':
            print('READY', flush=True)
            if not self.shutdown_requested.wait(5):
                raise RuntimeError('startup signal not delivered')
            return False
        return True

    def start_sip(self):
        self.sip_sock = SimpleNamespace(close=lambda: None)

    def log(self, *args):
        if args and str(args[0]).startswith('ready:'):
            print('READY', flush=True)

    def shutdown(self, *args, **kwargs):
        if os.environ.get('LINEATA_TEST_SIGNAL_PHASE') == 'teardown':
            self.shutdown_requested.clear()
            print('SHUTDOWN-ENTER', flush=True)
            if not self.shutdown_requested.wait(5):
                raise RuntimeError('second signal not delivered')
        self.stop.set()
        print('SHUTDOWN', flush=True)
        return 'deregister:ok'

module.LinearATA = SilentATA
sys.argv = ['ata', '--pbx', '127.0.0.1', '--local-ip', '127.0.0.1', '--login', 'test']
module.main()
'''


def run_signaled_child(signum, phase='ready'):
    env = dict(os.environ, LINEATA_SIP_PASSWORD='test-only',
               LINEATA_TEST_SIGNAL_PHASE=phase)
    child = subprocess.Popen(
        [sys.executable, '-c', CHILD], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    selector = selectors.DefaultSelector()
    try:
        selector.register(child.stdout, selectors.EVENT_READ)
        assert selector.select(10), 'child never reached ready state'
        assert child.stdout.readline().strip() == 'READY'
        child.send_signal(signum)
        if phase == 'teardown':
            assert selector.select(10), 'child never entered shutdown'
            assert child.stdout.readline().strip() == 'SHUTDOWN-ENTER'
            child.send_signal(signum)
        out, err = child.communicate(timeout=10)
        assert child.returncode == 0, err
        assert out.splitlines() == ['SHUTDOWN'], (out, err)
    finally:
        selector.close()
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGINT])
def test_actual_signal_reaches_main_shutdown_coordinator(signum):
    run_signaled_child(signum)


@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGINT])
def test_requested_signal_during_startup_is_a_normal_exit(signum):
    run_signaled_child(signum, phase='startup')


def test_second_sigterm_during_shutdown_remains_a_request():
    run_signaled_child(signal.SIGTERM, phase='teardown')


def test_signal_request_does_not_reenter_a_wait_condition_lock():
    # Force a signal at the critical point where Event.set() formerly tried
    # to reacquire the condition held by Event.wait(). A subprocess bounds a
    # regression to a short timeout and isolates signal state from pytest.
    code = r'''
import signal
from bridge.ata import LinearATA
ata = LinearATA({})
request = ata.shutdown_requested
sleeper = getattr(request, '_pause', request)
signal.signal(signal.SIGTERM, ata.request_shutdown)
with sleeper._cond:
    signal.raise_signal(signal.SIGTERM)
assert request.is_set()
print('REQUESTED', flush=True)
'''
    result = subprocess.run(
        [sys.executable, '-c', code], cwd=ROOT, capture_output=True,
        text=True, timeout=3,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'REQUESTED'
