# Experiment sources and local evidence

`experiments/` contains the bounded USB probes used during the independent
audit. They expect `ata.py` and its supporting modules on the import path
(the captured runs used isolated flat deployments on the Linux test host).

- `audit_usb.py`: resets/initializes the dongle and runs a heartbeat baseline.
  **`--ring` physically rings the phone.** Do not use it while silent testing
  is required. See the current audit for the operator-confirmed result.
- `silent_queue_probe.py`: uses the current runtime loop, blocks every runtime
  write except heartbeat, ignores handset events and exits after 20 seconds.
  It still resets and initializes the dongle; it is not a read-only check.
- `silent-queue-source.sha256`: hashes of the exact deployed source files from
  that experiment, not a claim that later source revisions are identical.
- `call_session.py`: bounded real USB/SIP/RTP call test with USB/network
  captures, a hidden secret prompt, control/event logs and orderly shutdown.
  It accepts real calls and can ring the phone. `--enable-candidate` records
  the historical missing-command experiment; use it with the pre-fix daemon
  only, since current code already schedules that audio-enable command.
- `native-call-source-2026-11-26.sha256`: source hashes for the verified paced
  incoming/outgoing and dial-tone sessions.

`remote-scripts/` preserves historical experiments, **not recommended test
procedures**. Several omit heartbeat, swallow disconnects or use concurrent
USB readers. They cannot isolate the causes claimed by the earlier handoff.
They can change line state, reset the dongle or ring the phone. Retained source
helps audit methodology without repeating the experiments.

`remote-baseline/` is local-only and ignored. It contains duplicate deployment
snapshots needed to interpret existing evidence. Raw captures under `captures/`
also stay local because they can contain audio and device identity.
