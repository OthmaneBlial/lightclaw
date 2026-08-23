# Real Telegram Verification

The deterministic stories run without credentials and are the CI contract. This checklist is the separate manual proof for Telegram delivery, provider latency, and a real delegated coding-agent boundary. Never substitute fixture output for this evidence.

## Preconditions

1. Install LightClaw in an isolated environment and run `lightclaw doctor --json`.
2. Use a private test bot and set exactly one numeric `TELEGRAM_ALLOWED_USERS` ID.
3. Use a disposable repository containing no secrets or personal data.
4. Start with `LOCAL_AGENT_SAFETY_MODE=workspace-write`; do not enable trusted execution for this check.
5. Record the LightClaw commit, Python version, host OS, provider/model, start/end time, and sanitized receipt paths.

Do not paste bot tokens, provider keys, Telegram IDs, prompts containing private data, or unsanitized receipts into issues or commits.

## Story A: memory across restart

1. Send: `Remember that my project launch code is Orchid-47.`
2. Wait for confirmation, stop LightClaw cleanly, and start it again.
3. Send: `What is my project launch code?`
4. Pass only if the reply contains `Orchid-47` and the local database timestamp predates the restarted process.

## Story B: bounded repository task

1. Select the disposable repository with `/workspace <absolute-path>`.
2. Send: `Add a health check to this tiny Python service, keep changes inside this workspace, and prove it with a unit test.`
3. Review the proposed paths, commands, risk level, and capability profile before approving.
4. Pass only if the final result includes a reviewable diff, a real passing test command, and a local receipt. Confirm that no file outside the disposable repository changed.

## Story C: two-lane approved plan

1. Send: `Research the release risks first. Then have a builder turn those findings into a launch checklist and audit the handoffs and final deliverable.`
2. Review and approve the two-lane DAG.
3. Pass only if the builder starts after the research handoff, both handoffs parse, a final artifact exists, and failures/retries are visible in the receipt.

## Evidence record

Keep one row per attempt. A blank result means the scenario has not been verified.

| Date | Commit | Story | Device/client | Provider/model | Duration | Result | Sanitized evidence |
|---|---|---|---|---|---:|---|---|
| — | — | — | — | — | — | Not run | — |

## Cleanup and rollback

Stop LightClaw, revoke the temporary bot token and provider key if one was created, inspect the run receipt, and remove only the registered disposable workspace. Use `lightclaw undo <task-id>` for a LightClaw-owned task workspace; do not manually delete an unreviewed path.
