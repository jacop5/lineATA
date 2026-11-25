# Audit index

Start with the [native call validation](docs/native-calls-2026-11-26.md).
The basic hardware call flow and two-way speech now work; regular clicking
was fixed. A brief analog-hangup transient remains an open quality issue.
The [takeover audit](docs/reaudit-session-2026-11-25.md) records the preceding
investigation, invalid prior experiments and software changes.

- [Paced native calls](docs/native-calls-paced-2026-11-26.json): verified
  incoming/outgoing USB and RTP timing.
- [Audio-enable experiment](docs/native-call-candidate-2026-11-26.json):
  two-way media before the pacing correction.
- [Takeover capture report](docs/reaudit-captures-2026-11-25.json): descriptor-filtered,
  paired USB completions, consistent time origin and incremental framing.
- [Historical handoff](HANDOFF.md): the previous implementation and experiments.
- [Historical audit](docs/prior-audit-2026-11-25.md): preserved for comparison;
  several conclusions and timestamps were corrected by the current audit.
- [Historical capture report](docs/capture-audit-2026-11-25.json): superseded;
  includes CDC data misclassified as malformed service frames.

Raw captures and duplicate remote deployment snapshots stay local and ignored.
Reproduce a structural report with `python3 tools/audit_captures.py CAPTURE.pcap`.
