# Durable Job Control

LightClaw stores approved job, lane, lease, heartbeat, retry, and event state in `jobs.db` beside the configured memory database. The parent runtime directory is private and the database uses owner-only permissions.

## Guarantees

- A SQLite partial unique index permits only one `running` or `cancel_requested` writer for a resolved workspace.
- Queued work is claimed by priority, then creation time.
- Every lane declares `idempotent` and `resumable`; unsafe lanes are explicitly non-resumable.
- Parallel lanes with overlapping owned path trees are rejected before execution. An overlap is allowed only when the DAG orders those lanes sequentially.
- Retry attempts are stored per lane and cannot exceed `max_attempts`.
- Running jobs heartbeat. Startup recovery and `lightclaw doctor` expose absent workers or stale heartbeats as `stalled`.

## Inspect the queue

```bash
lightclaw jobs list
lightclaw jobs list --status queued --json
lightclaw jobs status <run-id>
```

Telegram `/show` reports active, queued, and stalled counts without exposing job goals or Telegram identifiers.

## Control a job

```bash
lightclaw jobs cancel <run-id>
lightclaw jobs resume <run-id>
lightclaw jobs retry <run-id> --lane <failed-idempotent-lane>
```

A running cancellation becomes `cancel_requested`; the in-process heartbeat cancels the process tree and records `canceled`. Resume and retry fail closed for non-resumable or non-idempotent lanes. These commands never publish, push, or delete a workspace.
