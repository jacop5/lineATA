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

`remote-scripts/` preserves historical experiments, **not recommended test
procedures**. Several omit heartbeat, swallow disconnects or use concurrent
USB readers. They cannot isolate the causes claimed by the earlier handoff.
They can change line state, reset the dongle or ring the phone. Retained source
helps audit methodology without repeating the experiments.

`remote-baseline/` is local-only and ignored. It contains duplicate deployment
snapshots needed to interpret existing evidence. Raw captures under `captures/`
also stay local because they can contain audio and device identity.
