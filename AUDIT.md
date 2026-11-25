# Audit index

Start with the [current independent audit](docs/reaudit-session-2026-11-25.md)
for verified behavior, invalid prior experiments, changes, tests and remaining
hardware validation. The daemon is still experimental; native handset audio
and the combined physical call flow remain unverified.

- [Current capture report](docs/reaudit-captures-2026-11-25.json): descriptor-filtered,
  paired USB completions, consistent time origin and incremental framing.
- [Historical handoff](HANDOFF.md): the previous implementation and experiments.
- [Historical audit](docs/prior-audit-2026-11-25.md): preserved for comparison;
  several conclusions and timestamps were corrected by the current audit.
- [Historical capture report](docs/capture-audit-2026-11-25.json): superseded;
  includes CDC data misclassified as malformed service frames.

Raw captures and duplicate remote deployment snapshots stay local and ignored.
Reproduce a structural report with `python3 tools/audit_captures.py CAPTURE.pcap`.
