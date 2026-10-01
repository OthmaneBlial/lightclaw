# Durable Job Control

LightClaw stores approved job, lane, lease, heartbeat, retry, and event state in `jobs.db` beside the configured memory database. New runtime directories are owner-only; keep an existing parent directory private. Startup rejects a symlinked database path, restricts the database and existing SQLite WAL/SHM sidecars to owner-only before enabling WAL, and fails closed if it cannot. Read-only diagnostics also refuse symlinked database paths.

## Guarantees

- One run can execute per Telegram conversation at a time; separate conversations remain concurrent.
- Result acceptance and rejection use the workspace stored for the selected run ID. A newer result in the same conversation cannot redirect an already started decision into its workspace.
- A SQLite partial unique index permits only one `running` or `cancel_requested` writer for a resolved workspace.
- Stalled jobs also block queued work in their workspace until an operator cancels or resumes them.
- Queued work is claimed by priority, then creation time, among unlocked workspaces. A blocked workspace cannot stall the global queue for other workspaces.
- Agent launch requests claim only their own run when it reaches the queue head. If a launch cannot claim its run, it returns without an executor; queued jobs do not auto-start. Cancel stale queued jobs with `lightclaw jobs cancel <run-id>` and resubmit after the workspace is clear.
- Every lane declares `idempotent` and `resumable`; unsafe lanes are explicitly non-resumable.
- Parallel lanes with overlapping owned path trees are rejected before execution. An overlap is allowed only when the DAG orders those lanes sequentially.
- Retry attempts are stored per lane and cannot exceed `max_attempts`.
- Every worker invocation, including an automatic multi-agent repair, increments its durable lane attempt before launch. Counted starts require a running job, a queued lane, and an available attempt; duplicate or late starts after cancellation are refused without changing the lane.
- Running jobs heartbeat. Startup recovery marks a job `stalled` only after its worker process exits or its PID is reused, so a stale heartbeat cannot release a live workspace writer. Failed or permission-denied process probes retain the workspace lock; they do not prove the worker is gone. `lightclaw doctor` flags stale heartbeats for review.
- Process cleanup and stale-worker recovery check the observed job state and worker identity. A delayed recovery cannot stop or mark stalled a run that another instance has resumed with a new worker.

## Inspect the queue

```bash
lightclaw jobs list
lightclaw jobs list --status queued --json
lightclaw jobs status <run-id>
```

Telegram `/agent runs` shows ten jobs per page, with status and bounded goal previews for the current chat; **Previous** and **Next** navigate a stable snapshot, so new runs do not shift pages or their diff buttons. Private chats also get a page-scoped **Cancel queued run** button; group history stays read-only for queued jobs because requester IDs are not persisted. Run `/agent runs` again to include newer jobs. Multi-agent jobs include lane-state counts, and finished runs have a page- and chat-scoped **View diff** button that remains available after a bot restart. `/show` gives counts; neither command exposes other chats' activity.

## Control a job

```bash
lightclaw jobs cancel <run-id>
lightclaw jobs resume <run-id>
lightclaw jobs retry <run-id> --lane <failed-idempotent-lane>
```

A running cancellation becomes `cancel_requested`; the in-process heartbeat cancels the process tree and records `canceled`. Resume and retry fail closed for non-resumable or non-idempotent lanes. These commands never publish, push, or delete a workspace.

A lane retry also requires the job itself and the selected lane to be declared resumable. A refusal preserves the failed lane, its attempt count, and the job's terminal history.

Resume and retry clear the job's previous `finished_at` until it reaches a terminal state again. The original `started_at` remains available across attempts; events record the individual transitions.

Delegation prompts tell agents to keep subprocesses attached and stop temporary servers. Delegated POSIX processes wait for TERM/KILL cleanup and stream draining before cancellation returns; repeated cancellation requests do not interrupt cleanup or process-group registration/unregistration. An agent can ignore the prompt and detach a descendant, which may then outlive timeout or cancellation.

Cancellation also covers agent startup, before the process group is registered. Before output streaming begins, cleanup closes stdin and discards stdout/stderr in bounded chunks rather than retaining unread pipe buffers. Failed process-group registration still forces immediate termination.

Agent startup uses the same opened-directory launcher as acceptance commands. Replacing the workspace with a symlink before opening fails; replacing its path after opening cannot redirect the initial working directory. Codex uses this directory as its root, and the delegated prompt directs both agents to use relative paths rather than re-enter a moved workspace path. This preserves the existing agent permission profiles and does not add an OS sandbox.

Multi-agent preparation creates `AGENTS.md` atomically through the existing workspace directory handles and refuses an unexpected existing plan file. The handoff directory uses the same no-follow directory opener. A workspace or plan-file symlink cannot redirect this preparation into another directory.

## Scheduled Telegram updates and reminders

`/heartbeat` is available only in allowlisted private-chat mode. Public mode cannot inspect or control the process-wide scheduler or host-wide `HEARTBEAT.md`. In allowlisted mode, `/heartbeat on [minutes]` starts the single global scheduler for the chat that enables it (minimum five minutes). Intervals too large for the runtime are rejected without changing an existing schedule; `HEARTBEAT_INTERVAL_MIN` is validated at startup too. Ordinary messages and `/heartbeat show` do not change that destination. Running `/heartbeat on` again explicitly changes the destination and user memory scope and restarts the interval. `/heartbeat off` stops it. Authorized users share control of this scheduler.

Personality, heartbeat, and agent authentication text reads accept regular files and configured file aliases. Named pipes and other special files are rejected without waiting for their contents, preventing these reads from blocking the bot.

`/cron` manages reminders for the current Telegram chat. The bot must remain running to deliver them.

```text
/cron add every 30 Check the build
/cron add at 2026-10-01 09:00 Review the release notes
/cron list
/cron remove <id>
```

Date and time use the bot host's local timezone. `/cron` with no arguments lists reminders. Each reminder is scoped to its chat or forum-topic session, with a maximum of ten reminders per session. Remove one before adding after reaching the limit. Terminal and Telegram updates are serialized across processes. Long lists are split into messages that fit Telegram, preserving each job's identifier and full text.

Local times skipped by a daylight-saving change are rejected instead of shifted. If a clock change repeats a local time, the local form selects its first occurrence; use an ISO timestamp with an explicit UTC offset (such as `YYYY-MM-DDTHH:MM+01:00`) or Unix seconds to choose the other instant.

Reminder storage is limited to 1 MiB of serialized JSON, including escaped text and scheduling metadata. An addition that exceeds this limit is rejected before writing, preserving saved reminders. Remove old reminders to free space, then retry the addition.

Saved reminders are checked against the current Telegram access policy before delivery. Removing a private-chat user from `TELEGRAM_ALLOWED_USERS`, or disabling public group access, pauses delivery to that chat without deleting its jobs. Restoring access allows overdue reminders to run on the next scheduler check; remove unwanted jobs before restoring access.
