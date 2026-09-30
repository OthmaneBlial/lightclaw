# Durable Job Control

LightClaw stores approved job, lane, lease, heartbeat, retry, and event state in `jobs.db` beside the configured memory database. New runtime directories are owner-only; keep an existing parent directory private. Startup rejects a symlinked database path, restricts the database and existing SQLite WAL/SHM sidecars to owner-only before enabling WAL, and fails closed if it cannot. Read-only diagnostics also refuse symlinked database paths.

## Guarantees

- One run can execute per Telegram conversation at a time; separate conversations remain concurrent.
- A SQLite partial unique index permits only one `running` or `cancel_requested` writer for a resolved workspace.
- Stalled jobs also block queued work in their workspace until an operator cancels or resumes them.
- Queued work is claimed by priority, then creation time.
- Agent launch requests claim only their own run when it reaches the queue head. An older queued run remains queued rather than being marked running without an executor.
- Every lane declares `idempotent` and `resumable`; unsafe lanes are explicitly non-resumable.
- Parallel lanes with overlapping owned path trees are rejected before execution. An overlap is allowed only when the DAG orders those lanes sequentially.
- Retry attempts are stored per lane and cannot exceed `max_attempts`.
- Running jobs heartbeat. Startup recovery marks a job `stalled` only after its worker process exits or its PID is reused, so a stale heartbeat cannot release a live workspace writer. Failed or permission-denied process probes retain the workspace lock; they do not prove the worker is gone. `lightclaw doctor` flags stale heartbeats for review.

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

Agent startup uses the same opened-directory launcher as acceptance commands. Replacing the workspace with a symlink before opening fails; replacing its path after opening cannot redirect the initial working directory. Codex uses this directory as its root, and the delegated prompt directs both agents to use relative paths rather than re-enter a moved workspace path. This preserves the existing agent permission profiles and does not add an OS sandbox.

## Scheduled Telegram updates and reminders

`/heartbeat on [minutes]` starts the single global heartbeat scheduler for the chat that enables it (minimum five minutes). Intervals too large for the runtime are rejected without changing an existing schedule; `HEARTBEAT_INTERVAL_MIN` is validated at startup too. Ordinary messages and `/heartbeat show` do not change that destination. Running `/heartbeat on` again explicitly changes the destination and user memory scope and restarts the interval. `/heartbeat off` stops it. `HEARTBEAT.md` remains a host-wide file; authorized users share control of this scheduler.

Personality, heartbeat, and agent authentication text reads accept regular files and configured file aliases. Named pipes and other special files are rejected without waiting for their contents, preventing these reads from blocking the bot.

`/cron` manages reminders for the current Telegram chat. The bot must remain running to deliver them.

```text
/cron add every 30 Check the build
/cron add at 2026-10-01 09:00 Review the release notes
/cron list
/cron remove <id>
```

Date and time use the bot host's local timezone. `/cron` with no arguments lists reminders. Each reminder is scoped to the chat that created it. Long lists are split into messages that fit Telegram, preserving each job's identifier and full text.

Reminder storage is limited to 1 MiB of serialized JSON, including escaped text and scheduling metadata. An addition that exceeds this limit is rejected before writing, preserving saved reminders. Remove old reminders to free space, then retry the addition.

Saved reminders are checked against the current Telegram access policy before delivery. Removing a private-chat user from `TELEGRAM_ALLOWED_USERS`, or disabling public group access, pauses delivery to that chat without deleting its jobs. Restoring access allows overdue reminders to run on the next scheduler check; remove unwanted jobs before restoring access.
