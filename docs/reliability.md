# Software reliability evidence and offline test guide

This document preserves reviewed **software** evidence in version control. Hardware
claims belong only to the operator-confirmed captures documented in
[`native-calls-2026-11-26.md`](native-calls-2026-11-26.md) at commit
`e0f3727` (basic native single-line flow: boot/init, dial tone, inbound/outbound
calls, two-way PCMU speech, hangup both sides, successive calls, `270300` media
enable, 20 ms pacing). Software tests are **never hardware proof**; the
captured wire tables and media pacing are unchanged by the software rework.

## Running the offline suite

```sh
.re-env/bin/python -m pytest -q                      # full offline suite
.re-env/bin/python -m pytest tests/test_registration.py tests/test_registration_adversarial.py -q
.re-env/bin/python -m pytest tests/test_usb_lifecycle.py tests/test_usb_identity.py tests/test_boot_tty_selection.py tests/test_deployment_config.py -q
LINEATA_LOOPBACK_TEST=1 .re-env/bin/python -m pytest tests/test_rtp_loopback.py -q   # opt-in real UDP loopback
```

Run the commands against the revision being evaluated; passing results apply
only to the code and fault scenarios exercised. The optional UDP test uses
localhost sockets and simulated USB.

## Accepted registration corrections (software, reviewed)

REGISTER handling was reworked and lead-reviewed; the covered behaviors are:
own-Contact lease parsing (every `Contact:` header, comma-split respecting
`<...>`, case-folded whole-URI match, entry's own `expires=`, explicit `expires=0` kept as
0 and never reported positive, missing → `Expires:` fallback), bounded
backoff before exponentiation, 423 Min-Expires handling, retransmission
correlation (identical bytes at 0.5/1/2 s plus a bounded final response window,
never sending after stop), 401→`WWW-Authenticate` / 407→`Proxy-Authenticate`
status-specific challenges, provisional responses with a cached nonce never
yielding a fresh 'challenge', settled transactions immune to delayed/duplicate
responses, new request resetting per-transaction state. Counterexample tests in
`tests/test_registration.py` and `tests/test_registration_adversarial.py`
reproduce each defect before the fix and pass after.

**Known limitation:** Contact URIs with extra URI parameters that a PBX
rewrites do not match exactly; the code then falls back to the `Expires:`
header (documented compatibility limit, no SIP URI parser added).

## USB lifecycle status (do not overclaim)

- USB failure handling passed source review, deterministic regressions and
  independent fault-injection probes: claim failures, tty open/config/write
  errors, initialization disposal, cleanup on fault and normal stop, queue
  overflow stopping the daemon, serialized submit/cleanup and explicit call
  resource closure. These checks cover software failure paths only.
- The boot-tty identity helper (`bridge/usb_identity.py`,
  `tests/test_usb_identity.py`, `tests/test_boot_tty_selection.py`) is
  integrated and passes synthetic fixture tests, but **real Linux sysfs
  discovery and cold boot are unverified** (no host/device inspection was
  performed).
- **There is no in-process USB reconnect.** If the transport faults, the
  daemon's fatal handler stops the process; recovery today means process
  restart (the draft systemd unit's `Restart=always` is **unvalidated** —
  no Linux systemd validation has been done).
- **Graceful shutdown is not implemented:** `main()` catches only
  `KeyboardInterrupt`; no SIGTERM handler is installed, so SIGTERM terminates
  the process immediately and the `finally` block does not run; no
  `REGISTER expires=0` deregistration exists on any exit path.

## Deployment draft

[`deployment.md`](deployment.md) is an **offline draft** (systemd unit, env
example, udev rules). It is not validated on Linux (`systemd-analyze verify`
has not been run; no service was installed or activated) and must be installed from a reviewed commit, as described in its installation
instructions.

## Methodological corrections to earlier test claims

- `--help` short-circuits argparse, so passing values together with `--help`
  proves nothing about value parsing; integer acceptance is proven by
  parser-boundary probes with a guaranteed later error: `--pbx-port 5060
  --login` reaches the missing-login-value error, while `--pbx-port not-a-port
  --login` fails integer conversion first. Both exit before device or network
  access.
- Mocked retry tests must attach distinct fake devices and assert the exact
  disposal list; a fake returning success without reattaching made
  `dispose(None)` inflate disposal counts.
- Probabilistic thread-hammer overflow tests were replaced by deterministic
  producer/cleanup event-ordering tests.

## Still unvalidated

Broader PBX compatibility, the second FXS port, long-duration soak,
end-to-end USB reconnect/recovery, graceful SIGTERM deregistration, and any
live behavior of the new software changes (mock/synthetic tests only). The
brief hangup transient in the final USB audio frame remains an open hardware
question from the e0f3727 captures.
