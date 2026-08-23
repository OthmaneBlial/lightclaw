# Telegram Memory Story

This fixture proves one narrow claim: a known fact stored in SQLite can be recalled after the store is closed and reopened.

## Prerequisites

- Python 3.10–3.13
- LightClaw installed from this repository
- No Telegram account, bot token, or provider key

## Exact goal

> Remember that my project launch code is Orchid-47. Restart, then tell me the project launch code.

## Run the fixture

```bash
output="$(mktemp -d)/lightclaw-memory"
lightclaw demo --scenario memory --output "$output"
```

Expected evidence:

- `artifact/memory.db` contains the local persisted interaction;
- `artifact/recall.json` contains `Orchid-47` after a new `MemoryStore` opens the database;
- `receipt.json` and `receipt.md` report a passing `fact survives restart` check.

Typical fixture duration is under 2 seconds and cost is exactly $0 because no model is called. Actual Telegram/provider latency and cost are separate and depend on the user's provider.

## Cleanup

Review the receipt, then delete only the temporary output directory printed by the command.

## Security limits

This is a deterministic local memory fixture, not proof of Telegram delivery, provider availability, encryption at rest, or semantic quality on an arbitrary corpus. Anyone with access to the host account can read the SQLite database.
