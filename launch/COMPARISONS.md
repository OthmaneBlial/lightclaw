# Honest Product Comparisons

This is a fit table, not a benchmark or ranking. The LightClaw column describes the current
repository; other columns describe broad approaches whose exact behavior varies by product
and configuration. Verify a specific alternative in its current documentation before
making a decision.

| Need | LightClaw | Direct coding-agent CLI | Hosted agent platform | General self-hosted agent platform |
|---|---|---|---|---|
| Primary control surface | Telegram plus terminal | Terminal/editor, varies | Web/app, varies | Multiple channels, varies |
| Execution location | Local coding-agent process | Usually local; verify | Often vendor-managed; verify | Self-hosted; verify plugins/workers |
| Human plan approval | Explicit DAG approve/edit/deny | Tool-dependent | Product-dependent | Framework/config-dependent |
| Review evidence | Private receipt, checks, hashes, patch/branch | CLI-dependent | Product-dependent | Plugin-dependent |
| Recovery boundary | LightClaw-owned task workspace and checkpoint | Repository/tool-dependent | Product-dependent | Deployment-dependent |
| Runtime telemetry | None in LightClaw | Verify tool | Verify service | Verify deployment and extensions |
| Breadth | Intentionally Telegram-first and small | Focused coding workflow | Usually broader managed workflow | Usually broader/extensible workflow |
| Best fit | Self-hoster wanting phone review/control over local coding work | Developer already at a terminal/editor | Team wanting managed operations | Operator wanting broad customization |

## Claims LightClaw does not make

- It is not fully local inference when a hosted model is configured.
- A fixture demo does not prove live provider latency, price, availability, or quality.
- Permission manifests and regex command blocks are not a sandbox.
- “Self-hosted” does not remove Telegram, provider, or coding-CLI network boundaries.
- Current macOS benchmark results are not promises for every host or live workload.
- A short core is not automatically safer; the enforced controls and tests matter.

Compare verifiable contracts directly: [security model](../docs/THREAT_MODEL.md),
[provider matrix](../docs/generated/provider-compatibility.md), [runtime budget](../docs/ARCHITECTURE.md),
[raw benchmarks](../bench/README.md), and [private evidence rules](../docs/PRIVACY.md).

