"""Offline tests for tools/sip_lifecycle_probe.py — bounded mocks, no network.

Covers: the real hardware/call guards (refuse + counted, on_invite -> 480
only), the structural REGISTER-reply observer calling through to the real
handler, and the mocked coordinator lifecycle (run_lifecycle) success/failure
cleanup: fallback own-Contact cleanup runs only after the registrar is stopped
and joined, the listener is always stopped/joined and the socket closed even
if shutdown raised, and a PASS is impossible if any guard was attempted.
"""
import socket as real_socket
import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

import bridge.ata as ata_mod
from bridge.ata import sip_header
from sip_lifecycle_probe import SilentLifecycleProbe, run_lifecycle
import sip_lifecycle_probe as probe_mod

CFG = dict(pbx_ip='192.0.2.136', pbx_port=5060, domain='192.0.2.136',
           login='69', password='test-only', local_ip='10.0.0.2', sip_port=0)
ADDR = ('192.0.2.136', 5060)

INVITE = ('INVITE sip:69@192.0.2.136 SIP/2.0\r\n'
          'Via: SIP/2.0/UDP 192.0.2.136:5060;branch=z9hG4bKinv;rport\r\n'
          'From: <sip:99@192.0.2.136>;tag=c\r\nTo: <sip:69@192.0.2.136>\r\n'
          'Call-ID: cid-i\r\nCSeq: 1 INVITE\r\n'
          'Contact: <sip:99@192.0.2.136:5060>\r\nContent-Length: 0\r\n\r\n')


class FakeSock:
    def __init__(self):
        self.sent = []
        self.closed = False

    def bind(self, a):
        pass

    def settimeout(self, t):
        pass

    def getsockname(self):
        return ('10.0.0.2', 5099)

    def sendto(self, d, a):
        self.sent.append(d if isinstance(d, bytes) else d.encode())

    def recvfrom(self, n):
        raise real_socket.timeout()

    def close(self):
        self.closed = True

    def fileno(self):
        return -1 if self.closed else 7


@pytest.fixture
def fake_net(monkeypatch):
    socks = []

    def factory(*a, **k):
        s = FakeSock()
        socks.append(s)
        return s

    monkeypatch.setattr(ata_mod, 'socket', types.SimpleNamespace(
        AF_INET=real_socket.AF_INET, SOCK_DGRAM=real_socket.SOCK_DGRAM,
        timeout=real_socket.timeout, socket=factory))
    return socks


def make_probe():
    p = SilentLifecycleProbe(dict(CFG))
    p.log = lambda *a: None
    return p


def set_reg_attrs(p, cseq=2, branch='z9hG4bKabc'):
    """Attrs start_sip would set (no threads started)."""
    p.sip_sock = FakeSock()
    p.sip_stop = threading.Event()
    p.sip_local_port = 5099
    p.reg_call_id = 'cid-x'
    p.reg_cseq = cseq
    p.reg_branch = branch
    p.reg_settled = p.reg_responded = False
    p.reg_nonce = None
    p.reg_auth_header = 'Authorization'
    p.reg_challenged = p.reg_failed = p.reg_ok = False
    p.reg_event = threading.Event()
    p.reg_granted = None
    p.reg_min_expires = 0
    p.reg_last_requested = 300
    p.invite_transactions = {}
    p.invite_acks = {}
    p.final_responses = {}
    p.call = None
    return p


def reg_reply(cseq, branch, contact_line, code='200', reason='OK'):
    return (f'SIP/2.0 {code} {reason}\r\n'
            f'Via: SIP/2.0/UDP 10.0.0.2:5099;branch={branch};rport\r\n'
            'From: <sip:69@192.0.2.136>;tag=t\r\nTo: <sip:69@192.0.2.136>\r\n'
            f'Call-ID: cid-x\r\nCSeq: {cseq} REGISTER\r\n'
            f'{contact_line}\r\nContent-Length: 0\r\n\r\n')


def test_guards_refuse_hardware_and_calls(fake_net):
    p = make_probe()
    assert p.connect() is False and p.refused['connect'] == 1
    assert p.write('a00300') is None and p.refused['write'] == 1
    assert p.try_dial() is None and p.refused['try_dial'] == 1
    # on_invite emits ONLY a 480 (no FXS ring/answer state touched)
    p.sip_sock = FakeSock()
    p.sip_stop = threading.Event()
    p.sip_local_port = 5099
    p.on_invite(INVITE, ADDR)
    assert len(p.sip_sock.sent) == 1
    assert p.sip_sock.sent[0].startswith(b'SIP/2.0 480'), p.sip_sock.sent
    assert not p.ringing and p.call is None


def test_observer_records_structural_reg_replies_and_calls_through(fake_net):
    p = set_reg_attrs(make_probe())
    own = 'Contact: <sip:69@10.0.0.2:5099>;expires=60'
    p.handle_sip_message(reg_reply(2, 'z9hG4bKabc', own), ADDR)
    assert p.reg_ok is True                       # real handler ran unchanged
    obs = p.reg_replies[-1]
    assert obs == {'code': '200', 'own_contact_present': True,
                   'own_expires': 60, 'expires_header': None}, obs
    assert 'Authorization' not in repr(obs)       # no auth material observed
    # removal acknowledged vs contact-list absence
    p2 = set_reg_attrs(make_probe(), cseq=3, branch='z9hG4bKdef')
    other = 'Contact: <sip:other@10.0.0.9:5060>;expires=300'
    p2.handle_sip_message(reg_reply(3, 'z9hG4bKdef', other), ADDR)
    obs = p2.reg_replies[-1]
    assert obs['code'] == '200' and obs['own_contact_present'] is False, obs


def _fake_registrar_success(self):
    self.reg_ok = True
    self.reg_granted = 299
    self.reg_event.set()
    self.stop.wait(5)  # stand in until stop


def _fake_registrar_failure(self):
    self.reg_failed = True
    self.reg_event.set()
    self.stop.wait(5)


def test_lifecycle_success_mocked(fake_net, monkeypatch):
    monkeypatch.setattr(SilentLifecycleProbe, 'registrar', _fake_registrar_success)

    def fake_shutdown(self, timeout=10.0):
        self.shutdown_requested.set()
        self.reg_granted = None  # removal resets per-transaction grant state
        return 'usb-drained,deregister:ok'

    monkeypatch.setattr(SilentLifecycleProbe, 'shutdown', fake_shutdown)
    p = make_probe()
    r = run_lifecycle(p, reg_wait_s=2.0, shutdown_timeout_s=2.0, outer_s=10.0)
    assert r['registered'] is True and r['dereg'] == 'deregister:ok'
    assert r['granted_expires'] == 299
    assert r['cleanup'] == 'skipped (deregister:ok already confirmed)'
    assert r['listener_alive'] is False and r['registrar_alive'] is False
    assert r['sock_closed'] is True and r['no_calls'] and r['dev_none']
    assert r['ok'] is True and sum(r['refused'].values()) == 0


def test_lifecycle_failure_cleanup_runs_after_registrar_joined(fake_net, monkeypatch):
    monkeypatch.setattr(SilentLifecycleProbe, 'registrar', _fake_registrar_failure)
    monkeypatch.setattr(probe_mod, 'CLEANUP_S', 0.6)

    def fake_shutdown(self, timeout=10.0):
        self.shutdown_requested.set()
        return 'deregister:timeout'

    monkeypatch.setattr(SilentLifecycleProbe, 'shutdown', fake_shutdown)
    order = {}
    real_cleanup = probe_mod.own_contact_cleanup

    def checked_cleanup(ata, bound_s=1.0):
        # fallback must not race the registrar: stop set and thread joined first
        order['stop_set'] = ata.stop.is_set()
        order['registrar_alive_at_cleanup'] = bool(
            ata.registrar_thread and ata.registrar_thread.is_alive())
        return real_cleanup(ata, bound_s=bound_s)

    monkeypatch.setattr(probe_mod, 'own_contact_cleanup', checked_cleanup)
    p = make_probe()
    r = run_lifecycle(p, reg_wait_s=2.0, shutdown_timeout_s=2.0, outer_s=10.0)
    assert r['registered'] is False and r['dereg'] == 'deregister:timeout'
    assert order['stop_set'] is True and order['registrar_alive_at_cleanup'] is False
    assert r['cleanup'] not in ('not-run',) and 'skipped' not in r['cleanup']
    assert r['listener_alive'] is False and r['sock_closed'] is True
    assert r['ok'] is False                       # failure is never a PASS


def test_lifecycle_shutdown_raises_still_closes_listener(fake_net, monkeypatch):
    monkeypatch.setattr(SilentLifecycleProbe, 'registrar', _fake_registrar_success)

    def boom(self, timeout=10.0):
        raise RuntimeError('simulated shutdown failure')

    monkeypatch.setattr(SilentLifecycleProbe, 'shutdown', boom)
    p = make_probe()
    r = run_lifecycle(p, reg_wait_s=2.0, shutdown_timeout_s=2.0, outer_s=10.0)
    assert 'shutdown-error:RuntimeError' in r['result']
    assert r['listener_alive'] is False and r['registrar_alive'] is False
    assert r['sock_closed'] is True
    assert r['ok'] is False


def test_no_false_pass_if_a_guard_was_attempted(fake_net, monkeypatch):
    monkeypatch.setattr(SilentLifecycleProbe, 'registrar', _fake_registrar_success)

    def fake_shutdown(self, timeout=10.0):
        return 'deregister:ok'

    monkeypatch.setattr(SilentLifecycleProbe, 'shutdown', fake_shutdown)
    p = make_probe()
    p.write('a00300')                             # accidental hardware attempt
    r = run_lifecycle(p, reg_wait_s=2.0, shutdown_timeout_s=2.0, outer_s=10.0)
    assert r['registered'] and r['dereg'] == 'deregister:ok' and r['sock_closed']
    assert r['refused']['write'] == 1 and r['no_usb'] is False
    assert r['ok'] is False                       # guard attempt blocks PASS


def test_cleanup_exception_still_closes_socket_and_listener(fake_net, monkeypatch):
    monkeypatch.setattr(SilentLifecycleProbe, 'registrar', _fake_registrar_failure)
    monkeypatch.setattr(SilentLifecycleProbe, 'shutdown',
                        lambda self, timeout: 'deregister:timeout')

    def fail_cleanup(*a, **k):
        raise OSError('simulated cleanup failure')

    monkeypatch.setattr(probe_mod, 'own_contact_cleanup', fail_cleanup)
    p = make_probe()
    r = run_lifecycle(p, outer_s=2)
    assert r['cleanup'] == 'cleanup-error:OSError'
    assert r['sock_closed'] and not r['listener_alive']
    assert not r['registrar_alive'] and not r['ok']


def test_unstopped_registrar_prevents_fallback_transaction(fake_net, monkeypatch):
    class Unstoppable:
        def is_alive(self):
            return True

        def join(self, timeout):
            assert timeout == 0

    p = set_reg_attrs(make_probe())
    p.reg_ok = True
    p.registrar_thread = Unstoppable()
    p.sip_listener_thread = None
    monkeypatch.setattr(p, 'start_sip', lambda **kw: None)
    monkeypatch.setattr(p, 'shutdown', lambda timeout: 'deregister:timeout')

    def forbidden_cleanup(*a, **k):
        pytest.fail('shared transaction mutated while registrar was alive')

    monkeypatch.setattr(probe_mod, 'own_contact_cleanup', forbidden_cleanup)
    r = run_lifecycle(p, outer_s=0)
    assert r['cleanup'] == 'skipped (registrar still running)'
    assert r['sock_closed'] and r['registrar_alive'] and not r['ok']
