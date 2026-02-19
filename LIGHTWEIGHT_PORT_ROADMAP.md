# LightClaw Lightweight Port Roadmap

This document tracks practical upgrades while keeping LightClaw small, fast, and reliable.

## Core Rule (Keep It Light)

- Prefer single-file additions over new subsystems.
- Reuse existing `.lightclaw/*` paths; avoid new top-level runtime roots.
- Make every new feature optional and disabled by default.
- Add only if it improves reliability, safety, or user-visible quality.

## Prioritized Proposals

## Progress (Updated 2026-02-19)

- [DONE] P0.1 Atomic state writes for non-DB runtime files (`8acb95d`)
- [DONE] P0.2 Delegation preflight checks (`/agent doctor`) (`9ce48e9`)
- [DONE] P0.3 stronger workspace path/symlink guard
- [DONE] P0.4 delegation safety policy layer (`LOCAL_AGENT_SAFETY_MODE`, `LOCAL_AGENT_DENY_PATTERNS`)
- [SKIP] P0.5 per-chat task lock/queue for delegated runs
- [DONE] P1.6 Heartbeat automation via `HEARTBEAT.md`
- [SKIP] P2.11 Migration command with dry-run
- [SKIP] P2.12 Optional built-in web tools (`web_search`, `web_fetch`)

## P0 (High Impact, Low-Medium Effort)

### 1) Atomic state writes for non-DB runtime files [DONE]
- Why: prevent corruption on crash/power loss.
- Scope:
  - Add shared helper `atomic_write_text(path, content)` (tmp + fsync + rename).
  - Use it for JSON state files (skills state, delegation state, future runtime state files).
- Effort: low.

### 2) Delegation preflight checks (`/agent doctor`) [DONE]
- Why: fail fast with clear action if local CLI auth is missing/expired.
- Scope:
  - New `/agent doctor` command.
  - Detect installed CLIs, version, auth status (best-effort), executable path.
  - Return actionable fixes (`codex login`, `claude setup-token`, `opencode auth login`).
- Effort: low.

### 3) Stronger workspace path/symlink guard for all write/edit paths [DONE]
- Why: close edge cases for symlink escape and non-existing-path ancestor tricks.
- Scope:
  - Reuse current path policy but add explicit existing-ancestor symlink checks.
  - Apply to every file op and repair pass path resolution.
- Effort: low-medium.

### 4) Execution safety policy layer for delegated local agents [DONE]
- Why: local agents run with broad permissions; add guardrails at LightClaw layer.
- Scope:
  - Add optional safety mode env flags:
  - `LOCAL_AGENT_SAFETY_MODE=strict|off`
  - `LOCAL_AGENT_DENY_PATTERNS=...`
  - Block obviously destructive tasks before CLI dispatch.
- Current boundary:
  - This policy currently applies to delegated local-agent runs only (`/agent run`, `/agent use` mode path).
  - Normal non-delegated LLM chat path is intentionally unchanged for now.
- Effort: medium.

### 5) Per-chat task lock/queue for delegated runs [SKIP]
- Why: avoid race conditions when user sends multiple heavy tasks quickly.
- Scope:
  - One active delegated run per session/chat.
  - New run policy: reject with message or queue next task.
- Effort: medium.

## P1 (High Value, Medium Effort)

### 6) Heartbeat automation via `HEARTBEAT.md` [DONE]
- Why: proactive automation without constant prompts.
- Scope:
  - Optional scheduler reads `.lightclaw/workspace/HEARTBEAT.md`.
  - Runs every N minutes (min 5), sends updates to last active chat.
  - New commands: `/heartbeat on|off|show`.
- Effort: medium.

### 7) Minimal cron jobs (`/cron`) backed by JSON store
- Why: explicit scheduled reminders/tasks users can control.
- Scope:
  - Keep only `every` and `at` modes initially (skip full cron expr first).
  - Store in `.lightclaw/cron/jobs.json`.
  - Commands: `/cron add`, `/cron list`, `/cron remove`.
- Effort: medium.

### 8) Health/readiness HTTP endpoint
- Why: production/dev observability and uptime checks.
- Scope:
  - Optional tiny HTTP server:
  - `/health` (always up if process alive)
  - `/ready` (provider/channel checks)
  - Controlled by env vars (`HEALTH_ENABLED`, `HEALTH_PORT`).
- Effort: medium.

### 9) Structured JSON logging mode
- Why: easier debugging and future dashboard integration.
- Scope:
  - Keep current human logs as default.
  - Optional dual output JSON file (`.lightclaw/logs/lightclaw.jsonl`).
  - Add per-event fields for session/channel/operation.
- Effort: medium.

## P2 (Nice-to-Have, Still Lightweight If Scoped)

### 10) Multi-layer skill discovery precedence
- Why: lets advanced users keep reusable global skills.
- Scope:
  - Resolve skills in order: workspace > global > built-in template dir.
  - Preserve existing behavior for current users.
- Effort: medium.

### 11) Migration command with dry-run [SKIP]
- Why: safe upgrades when workspace/runtime format evolves.
- Scope:
  - Add `lightclaw migrate --dry-run` first.
  - Show copy/backup plan, then apply with confirmation.
- Effort: medium.

### 12) Optional built-in web tools (`web_search`, `web_fetch`) [SKIP]
- Why: better autonomous research with explicit tool boundaries.
- Scope:
  - Keep optional by env toggle.
  - Use a single provider path first (DuckDuckGo or Brave key if set).
- Effort: medium.

## Recommended Execution Order

1. [DONE] P0.1 Atomic writes
2. [DONE] P0.2 `/agent doctor`
3. [DONE] P0.3 path/symlink hardening
4. [DONE] P0.4 delegation safety policy
5. [SKIP] P0.5 per-chat lock/queue
6. [DONE] P1.6 heartbeat
7. P1.8 health endpoint
8. P1.7 cron
9. P1.9 JSON logs
10. [SKIP] P2.11 migrate command (dry-run)
11. [SKIP] P2.12 web tools
12. P2.10 skill discovery precedence (as needed)

## What We Should Not Port (to stay lightweight)

- Full multi-channel manager (Discord/Slack/etc) right now.
- Full message bus architecture rewrite.
- Full multi-agent swarm orchestration.
- Large config refactor from `.env` to full JSON hierarchy.

## Definition of Done (for each proposal)

- Feature is optional and off by default.
- No regression in Telegram-first flow.
- No major startup-time increase.
- Clear command/help/docs update.
- At least one focused test or smoke test per feature.
