# Durable Job Control

LightClaw stores approved job, lane, lease, heartbeat, retry, and event state in `jobs.db` beside the configured memory database. New runtime directories are owner-only; keep an existing parent directory private. Startup rejects a symlinked database path, restricts the database and existing SQLite WAL/SHM sidecars to owner-only before enabling WAL, and fails closed if it cannot. Read-only diagnostics also refuse symlinked database paths.

## Guarantees

- One run can execute per Telegram conversation at a time; separate conversations remain concurrent.
- A SQLite partial unique index permits only one `running` or `cancel_requested` writer for a resolved workspace.
- Stalled jobs also block queued work in their workspace until an operator cancels or resumes them.
- Queued work is claimed by priority, then creation time.
- Every lane declares `idempotent` and `resumable`; unsafe lanes are explicitly non-resumable.
- Parallel lanes with overlapping owned path trees are rejected before execution. An overlap is allowed only when the DAG orders those lanes sequentially.
- Retry attempts are stored per lane and cannot exceed `max_attempts`.
- Running jobs heartbeat. Startup recovery marks a job `stalled` only after its worker process exits, so a stale heartbeat cannot release a live workspace writer. `lightclaw doctor` flags stale heartbeats for review.

## Inspect the queue

```bash
lightclaw jobs list
lightclaw jobs list --status queued --json
lightclaw jobs status <run-id>
```

Telegram `/show` reports active, queued, and stalled counts for the current chat only; it does not expose job goals or other chats' activity.

## Control a job

```bash
lightclaw jobs cancel <run-id>
lightclaw jobs resume <run-id>
lightclaw jobs retry <run-id> --lane <failed-idempotent-lane>
```

A running cancellation becomes `cancel_requested`; the in-process heartbeat cancels the process tree and records `canceled`. Resume and retry fail closed for non-resumable or non-idempotent lanes. These commands never publish, push, or delete a workspace.

Delegated POSIX processes wait for TERM/KILL cleanup and stream draining before cancellation returns. Repeated cancellation requests do not interrupt that cleanup or a pending process-group registration/unregistration. Detached descendants can still escape the process group.

Cancellation also covers agent startup, before the process group is registered. Before output streaming begins, cleanup closes stdin and discards stdout/stderr in bounded chunks rather than retaining unread pipe buffers. Failed process-group registration still forces immediate termination.

## Scheduled Telegram updates and reminders

`/heartbeat on [minutes]` starts the single global heartbeat scheduler for the chat that enables it (minimum five minutes). Ordinary messages and `/heartbeat show` do not change that destination. Running `/heartbeat on` again explicitly changes the destination and user memory scope and restarts the interval. `/heartbeat off` stops it. `HEARTBEAT.md` remains a host-wide file; authorized users share control of this scheduler.

`/cron` manages reminders for the current Telegram chat. The bot must remain running to deliver them.

```text
/cron add every 30 Check the build
/cron add at 2026-10-01 09:00 Review the release notes
/cron list
/cron remove <id>
```

Date and time use the bot host's local timezone. `/cron` with no arguments lists reminders. Each reminder is scoped to the chat that created it.
