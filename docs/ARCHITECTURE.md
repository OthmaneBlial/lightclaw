# LightClaw Architecture

LightClaw is a Telegram-first control surface around hosted model providers and local coding-agent CLIs. The public top-level modules remain compatibility facades; stable implementation domains live under `core/`.

## Runtime domains

| Domain | Owner | Does not own |
|---|---|---|
| Provider protocol | `core/llm/` | Telegram handlers, persistence, vendor SDK behavior outside adapters |
| Telegram command routing | `core/bot/commands/agent_router.py` | Plan normalization, worker acceptance, durable execution |
| Multi-agent planning | `core/bot/delegation/planning.py` | Process execution, job state, acceptance |
| Worker task contracts | `core/bot/delegation/tasks.py` | Scheduler state or process lifecycle |
| Acceptance | `core/bot/commands/agent_acceptance.py` | Planning and worker execution |
| Durable execution coordination | `core/bot/commands/agent_execution.py` | Acceptance rules and plan normalization |
| Local process execution | `core/bot/delegation/execution.py` | Telegram command routing and planning |
| Job persistence | `core/jobs.py` | UI rendering and model planning |
| Receipts/artifacts | `core/receipts.py`, `core/artifacts.py` | Execution authorization |
| Filesystem state primitives | `core/fs.py` | Domain policy |

Compatibility compositions in `core/bot/commands/agent.py` and `core/bot/delegation/multi.py` preserve existing mixin imports without recombining implementation responsibilities.

## Enforced budget

The versioned [core budget](architecture/core-budget.json) caps total runtime lines, module/function size, AST branch points, direct dependencies, cold-start p95, and wheel size. [Module boundaries](architecture/module-boundaries.json) assert separate planning, execution, persistence, rendering, and acceptance owners.

```bash
python scripts/check_architecture.py --check
python -m bench.runtime_footprint --output /tmp/lightclaw-runtime-footprint.json
```

CI rejects stale metrics and budget overruns. Raising a limit requires an explicit architecture rationale; limits are not silently moved to make a change pass. Release builds attach a machine-readable runtime footprint containing cold-start samples, direct dependency list, and wheel size.

## Decisions

- [ADR-0001: Security boundaries](adr/0001-security-boundaries.md)
- [ADR-0002: Durable job persistence](adr/0002-durable-job-persistence.md)
- [ADR-0003: Private run receipts](adr/0003-private-run-receipts.md)
- [ADR-0004: Namespaced bounded memory](adr/0004-namespaced-bounded-memory.md)
- [ADR-0005: Permissioned skills](adr/0005-permissioned-skills.md)
