# Telegram Multi-Agent Story

This fixture proves the orchestration contract: a two-lane DAG, a machine-readable handoff, a downstream artifact, and a final audit.

## Prerequisites

- Python 3.10–3.13
- LightClaw installed from this repository
- No Telegram account, coding-agent CLI, bot token, or provider key

## Exact goal

> Research the release risks first. Then have a builder turn those findings into a launch checklist and audit the handoffs and final deliverable.

## Run the fixture

```bash
output="$(mktemp -d)/lightclaw-multi-agent"
lightclaw demo --scenario multi-agent --output "$output"
```

Expected evidence:

- `artifact/AGENTS.md` declares `builder` after `research`;
- `artifact/handoff/research.json` feeds `artifact/handoff/builder.json`;
- `artifact/launch-checklist.md` is the final deliverable;
- `artifact/final-audit.json` records dependency order, handoff validity, and artifact existence;
- the receipt contains three passing acceptance checks.

Typical fixture duration is under 2 seconds and cost is exactly $0. Live parallel workers can take minutes and their cost depends on the selected tools/providers.

## Cleanup

Review the local receipt, then delete only the printed temporary output directory.

## Security limits

This scenario simulates worker outputs deterministically; it proves LightClaw's fixture contract, not the correctness of external models. Real multi-agent plans require human approval and can still produce incorrect or harmful workspace changes within granted scope.
