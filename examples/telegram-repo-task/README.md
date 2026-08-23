# Telegram Repository Task Story

This fixture reproduces the core LightClaw promise without remote services: a recorded phone/Telegram request and approval become a scoped Git change, an actual test run, a review patch, and a receipt.

## Prerequisites

- Python 3.10–3.13
- LightClaw installed from this repository
- No Telegram account, coding-agent CLI, bot token, or provider key

## Exact goal

> Add a health check to the tiny Python service, keep the change inside the fixture, and prove it with a unit test.

## Run the fixture

```bash
output="$(mktemp -d)/lightclaw-repo-task"
lightclaw demo --scenario repo-task --output "$output"
```

Expected evidence:

- `artifact/service.py` exposes `health()`;
- `artifact/test_service.py` checks the exact response;
- LightClaw really runs `python -m unittest -v` in a minimal environment;
- `phone-to-patch.json` records the request, preview, approval, verified result, and zero hidden manual steps;
- `review/changes.patch` is a standard Git patch and `review/artifact.json` records its base, branch, paths, and SHA-256;
- `artifact/test-output.txt` and both receipt formats record the result;
- no branch is pushed and no pull request is opened.

Typical fixture duration is under 2 seconds and cost is exactly $0. A real delegated coding task can take minutes and can incur provider or coding-agent subscription cost.

## Cleanup

Review the receipt, then delete only the printed temporary output directory. The demo refuses to run in a non-empty directory.

## Security limits

The fixture worker is built into LightClaw and does not exercise Codex/Claude authentication or their sandbox implementations. Real tasks still require scope review and should use `observe` or `workspace-write`, not `trusted-command`, whenever possible.

Use the separate [real Telegram verification checklist](../../docs/MANUAL_VERIFICATION.md#story-b-bounded-repository-task) when testing an actual bot, provider, and coding-agent CLI.
