# lineATA project instructions

## Objective and architecture
Build a native Keenetic KN-3110 USB-to-FXS daemon for a generic SIP PBX. The active implementation is `bridge/ata.py`, supported by `transport.py`, `framing.py`, `media.py` and the captured initialization in `capture_init.py`. Do not use the proprietary nvox oracle as the production driver. Other bridge modules are historical experiments; consult README.md, AUDIT.md and docs/ before using their assumptions.

## Development
Use Python 3.10+ and the recorded dependencies. Run `.re-env/bin/python -m pytest -q` (or the project venv equivalent). Opt-in synthetic UDP integration: `LINEATA_LOOPBACK_TEST=1 .re-env/bin/python -m pytest tests/test_rtp_loopback.py -q`. These tests use simulated USB and cannot validate physical ringing or speech.

## Hardware and methodology
Do not run hardware experiments, contact the PBX, place calls, or access deployment secrets unless the project task explicitly authorizes it. Never store real credentials in source, process arguments, logs or committed artifacts. Keep raw audio/captures local; promote reviewed structural evidence to docs/tests.

Read the handoff and independent audits critically. Preserve captures and distinguish observed behavior from hypotheses. Do not repeat valid experiments without a methodological reason. Preserve single-owner USB I/O, matching control replies, documented frame bytes and media pacing unless reproducible evidence supports a change. Chip-family/port assumptions must be checked against device evidence.

## Acceptance and repository conventions
Verify relevant tests and actual diffs. Hardware acceptance requires real ringing, dial tone, two-way speech, local/remote hangup and repeated calls; mocks and ACKs alone are insufficient. Keep native behavior and known limitations accurately documented. Do not delete legacy evidence, overwrite unrelated user changes, or commit local secrets/runtime artifacts. Deployment examples are drafts until validated on the target; do not activate services or change passthrough without authorization.
