#!/usr/bin/env python3
"""Silent PBX lifecycle probe: real LinearATA SIP registration + shutdown.

Exercises the daemon's actual start_sip(register=True)/registrar/shutdown
path against a PBX with NO USB and NO calls:
- hardware exclusion is by subclass guards only (write/try_dial/connect
  refuse; on_invite answers 480 without FXS control); registration behavior
  itself is untouched;
- waits bounded <=12 s for reg_ok, then request_shutdown() + shutdown(timeout=10)
  and requires deregister:ok plus listener/registrar stopped and socket closed;
- the finally fallback ALWAYS attempts own-Contact cleanup (REGISTER expires=0,
  this Contact only — never wildcard) even if registration failed, but only
  AFTER the registrar thread is stopped and joined, so cleanup can never race
  the registrar over shared registration transaction fields; the listener is
  always stopped/joined and the socket closed even if shutdown raised;
- structural output only: status codes and own-Contact presence/expiry from
  REGISTER replies — never auth headers, message bodies or the secret.

Config: --pbx --login --local-ip [--port]; secret via getpass only.
Run under an outer `timeout 30`.
"""
import argparse
import getpass
import re
import sys
import threading
import time
from pathlib import Path

PROJECT = next((p for p in Path(__file__).resolve().parents
                if (p / 'bridge/ata.py').is_file()), Path.cwd())
sys.path.insert(0, str(PROJECT))
from bridge.ata import LinearATA, sip_header

REG_WAIT_S = 12.0
SHUTDOWN_TIMEOUT_S = 10.0
CLEANUP_S = 5.0
OUTER_S = 30.0


class SilentLifecycleProbe(LinearATA):
    """Hardware/call exclusion only — no registration-handling changes."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.reg_replies = []          # structural REGISTER reply observations
        self.refused = {'write': 0, 'try_dial': 0, 'connect': 0}

    # ---- guards: refuse anything that could touch hardware or place calls
    def write(self, hexstr, timeout=2000, guard=None):
        self.refused['write'] += 1
        self.log('probe: USB write refused')
        return None

    def try_dial(self):
        self.refused['try_dial'] += 1
        self.log('probe: try_dial refused')
        return None

    def connect(self):
        self.refused['connect'] += 1
        self.log('probe: USB connect refused')
        return False

    def on_invite(self, text, addr):
        # 480 only; never touches FXS ring/answer state.
        self.send_response(text, addr, 480, 'Temporarily Unavailable')

    # ---- observation only: record structural REGISTER reply facts, then the
    # real handler runs unchanged.
    def handle_sip_message(self, text, addr):
        if text.startswith('SIP/2.0') and 'REGISTER' in sip_header(text, 'CSeq'):
            code = text.split(' ', 2)[1] if len(text.split(' ', 2)) > 1 else '?'
            own_uri = f"sip:{self.cfg['login']}@{self.cfg['local_ip']}:{self.sip_local_port}"
            entries = []
            for value in re.findall(r'^Contact:[ \t]*(.+)$', text, re.M | re.I):
                entries.extend(self._split_contact_entries(value))
            own = [e for e in entries if own_uri in e]
            own_expires = None
            if own:
                m = re.search(r'expires=(\d+)', own[0], re.I)
                own_expires = int(m.group(1)) if m else None
            exp_hdr = re.search(r'^Expires:[ \t]*(\d+)', text, re.M | re.I)
            self.reg_replies.append({
                'code': code,
                'own_contact_present': bool(own),
                'own_expires': own_expires if own else None,
                'expires_header': int(exp_hdr.group(1)) if exp_hdr else None,
            })
        return super().handle_sip_message(text, addr)


def own_contact_cleanup(ata, bound_s=CLEANUP_S):
    """Best-effort own-Contact removal (expires=0, this Contact only) with its
    own absolute deadline. Caller must have stopped/joined the registrar first.
    Returns 'ok' only if a matching 200 acknowledged the removal."""
    sock = getattr(ata, 'sip_sock', None)
    if sock is None or ata.sip_stop.is_set():
        return 'skipped (listener already stopped)'
    deadline = time.monotonic() + bound_s
    try:
        msg = ata.registration_request(0)
    except (ValueError, AttributeError) as exc:
        return f'build-failed ({exc.__class__.__name__})'
    ata.reg_event.clear()
    outcome = ata._register_transaction(msg.encode(), stop_event=threading.Event(),
                                        deadline=deadline)
    if outcome == 'challenge' and time.monotonic() < deadline:
        try:
            msg = ata.registration_request(0)
        except ValueError:
            return 'auth-build-failed'
        ata.reg_event.clear()
        outcome = ata._register_transaction(msg.encode(), stop_event=threading.Event(),
                                            deadline=deadline)
    return outcome


def _stop_join(thread, deadline):
    if thread is None or thread is threading.current_thread():
        return True
    if thread.is_alive():
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return not thread.is_alive()


def run_lifecycle(ata, reg_wait_s=REG_WAIT_S, shutdown_timeout_s=SHUTDOWN_TIMEOUT_S,
                  outer_s=OUTER_S):
    """Bounded silent lifecycle. Returns a structural result dict; never raises
    out of the finally cleanup. Does not reimplement any daemon logic."""
    outer = time.monotonic() + outer_s
    result = ''
    registered = False
    granted = None
    dereg = 'deregister:?'
    cleanup = 'not-run'
    try:
        ata.start_sip(register=True)                    # real listener + registrar
        deadline = min(outer, time.monotonic() + reg_wait_s)
        while time.monotonic() < deadline and not ata.reg_ok and not ata.reg_failed:
            ata.stop.wait(0.05)
        registered = bool(ata.reg_ok)
        granted = ata.reg_granted  # shutdown starts a new REGISTER transaction
        ata.request_shutdown()                          # real signal-path entry
        try:
            result = ata.shutdown(timeout=min(shutdown_timeout_s,
                                              max(0.1, outer - time.monotonic())))
        except Exception as exc:
            result = (result + ',' if result else '') + f'shutdown-error:{exc.__class__.__name__}'
        dereg = next((p for p in result.split(',') if p.startswith('deregister:')),
                     'deregister:?')
    finally:
        # Stop and join the registrar BEFORE any fallback cleanup touches the
        # shared registration transaction fields (reg_cseq/reg_branch/reg_event).
        ata.stop.set()
        try:
            stopped = _stop_join(getattr(ata, 'registrar_thread', None), outer)
            if not stopped:
                cleanup = 'skipped (registrar still running)'
            elif 'deregister:ok' not in result:
                cleanup = own_contact_cleanup(
                    ata, bound_s=min(CLEANUP_S, max(0.0, outer - time.monotonic())))
            else:
                cleanup = 'skipped (deregister:ok already confirmed)'
        except Exception as exc:
            cleanup = 'cleanup-error:' + exc.__class__.__name__
        finally:
            ata.sip_stop.set()
            sock = getattr(ata, 'sip_sock', None)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            _stop_join(getattr(ata, 'sip_listener_thread', None), outer)

    listener_alive = bool(ata.sip_listener_thread and ata.sip_listener_thread.is_alive())
    registrar_alive = bool(ata.registrar_thread and ata.registrar_thread.is_alive())
    sock_closed = sock is None or sock.fileno() == -1
    no_calls = ata.call is None and not ata.invite_transactions and not ata.dialing
    no_usb = ata.dev is None and sum(ata.refused.values()) == 0
    ok = bool(registered and dereg == 'deregister:ok' and not listener_alive
              and not registrar_alive and sock_closed and no_calls and no_usb)
    return dict(registered=registered, result=result, dereg=dereg, cleanup=cleanup,
                granted_expires=granted, listener_alive=listener_alive,
                registrar_alive=registrar_alive, sock_closed=sock_closed,
                no_calls=no_calls, no_usb=no_usb, dev_none=ata.dev is None,
                refused=dict(ata.refused), reg_replies=list(ata.reg_replies), ok=ok)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--pbx', required=True)
    ap.add_argument('--login', required=True)
    ap.add_argument('--local-ip', required=True)
    ap.add_argument('--port', type=int, default=5060)
    args = ap.parse_args()
    password = getpass.getpass('Extension secret: ')   # never via argv/logs

    cfg = dict(pbx_ip=args.pbx, pbx_port=args.port, domain=args.pbx,
               login=args.login, password=password, local_ip=args.local_ip,
               sip_port=0)
    ata = SilentLifecycleProbe(cfg)
    r = run_lifecycle(ata)
    print(f'REGISTER {"PASS" if r["registered"] else "FAIL"} '
          f'granted_expires={r["granted_expires"]}', flush=True)
    print(f'SHUTDOWN {r["result"]}', flush=True)
    print(f'DEREG {"PASS" if r["dereg"] == "deregister:ok" else "FAIL " + r["dereg"]}',
          flush=True)
    print(f'CLEANUP {r["cleanup"]}', flush=True)
    print(f'STRUCT listener_alive={r["listener_alive"]} registrar_alive={r["registrar_alive"]} '
          f'sock_closed={r["sock_closed"]} no_calls={r["no_calls"]} dev_none={r["dev_none"]} '
          f'refused={r["refused"]}', flush=True)
    print('REG_REPLIES ' + repr(r['reg_replies']), flush=True)   # structural only
    print(f'RESULT {"PASS" if r["ok"] else "FAIL"}', flush=True)
    sys.exit(0 if r['ok'] else 1)


if __name__ == '__main__':
    main()
