# LightClaw Threat Model

This document describes the current alpha security boundary. It is intentionally narrower than a formal independent security audit.

## Assets and trust boundaries

The protected assets are provider credentials, the Telegram bot token, private conversation and memory data, user workspaces, delegated-agent output, and the host account running LightClaw.

Data crosses four important boundaries:

1. Telegram transports an identity and untrusted message content to the bot.
2. Hosted model providers receive configured prompts and context.
3. Local coding-agent CLIs receive a scoped task, minimal process environment, and task workspace.
4. Skills and generated artifacts influence prompts and may influence subsequent execution.

LightClaw memory and task workspaces are local by default. Model inference is not local when a hosted provider is configured.

## Threats and controls

| Threat | Current control | Residual risk |
|---|---|---|
| Unauthorized Telegram user | Numeric allowlist fails closed; allowlisted accounts work only in private chats; public chat access requires an explicit acknowledgement; privileged commands are rate-limited | A compromised allowed Telegram account remains authorized |
| Provider cost bursts | Text messages are limited to 20 per Telegram user per minute before model processing; voice transcription is limited to six requests per user per minute | Limits are process-local and reset on restart; use provider spending controls for account-wide limits |
| Group chat data disclosure | Group chats are rejected in allowlisted mode because members share the conversation and approval state | The explicit public override permits group chats; every group member can see bot replies and interact with shared state |
| Prompt injection | Human plan/privilege confirmation, restrictive capability profiles, bounded task workspaces | A model can still make harmful changes inside granted scope |
| Secret theft by delegated worker | Minimal environment allowlist with relative `PATH` entries removed; credential redaction in logs/results; no parent environment copy | Secrets already stored in readable workspace files remain visible to a worker |
| Secret disclosure through automatic edit retries | Common credential paths and detectable secret-bearing file contents are omitted from automatic retry/repair prompts; generated file blocks refuse common credential paths | Novel secret formats can evade detection; configured local coding-agent CLIs retain their own workspace access |
| Workspace file operation races | Bounded reads and writes and private metadata-directory creation walk workspace components through no-follow directory descriptors; writes compare the current content and mode with the read snapshot, and new-file creation refuses a late path | A host write after the final snapshot check can still race atomic replacement; LightClaw is not an OS sandbox |
| Oversized or reordered voice uploads | Declared files over 20 MB are rejected before download; oversized downloaded data is discarded, and only the latest per-chat request can create a pending approval | Unknown file sizes rely on the Telegram API download limit and are checked after download; stale in-flight transcription may still finish at Groq before its result is discarded |
| Delegated CLI and acceptance output exhaustion | Agent and acceptance streams are drained while each line is capped at 1 MiB and capture retains at most 2 Mi characters and 4,096 lines per stream | Final response may be truncated; review workspace changes and the run receipt |
| Workspace escape | Resolved non-symlink root, per-task direct child directory, task-scoped starting Git identity and artifact commands, acceptance paths and command working directories resolved within the workspace, external ownership record, external CLI sandbox flags | Trusted host execution intentionally removes this protection |
| Acceptance command host access | Commands and working directories are shown before approval; child environments exclude credentials; acceptance paths stay within the workspace | Acceptance commands are not OS-sandboxed by LightClaw and retain host-user permissions; use an OS/container boundary for untrusted repositories |
| Destructive rollback | Undo reads its ownership record through no-follow directory descriptors and removes one task relative to the opened workspace root; preview is the default | A host process with the same account can still change workspace contents during removal; files copied elsewhere by trusted execution are outside the undo boundary |
| Orphan child process | Delegated agents and POSIX acceptance checks use separate process groups with TERM/KILL on timeout or cancellation; active groups are registered for restart recovery | Detached descendants can still escape; non-POSIX process trees are not managed |
| Malicious skill inputs | Bounded archive parsing, encrypted/ambiguous bundle rejection, permission-manifest validation, inactive install, pinned provenance, and source-plus-manifest hash approval; legacy tree migration reads regular files through no-follow descriptors and caps each copy at 5 MiB | Reviewed prompt guidance can still be malicious; manifests are not a security audit |
| Credential leakage in errors | Known-value, assignment, bearer, and Telegram-token redaction; configured secrets are scrubbed from Telegram network and voice-download errors before logging | Novel secret formats or secrets shorter than four characters may evade redaction |
| Dependency compromise | Pinned GitHub Actions, local dependency audit, and active Dependabot updates; GitHub CI, CodeQL, OpenSSF Scorecard, and pull-request dependency review are disabled | New dependency or workflow vulnerabilities may go undetected until local review; registry and maintainer compromise cannot be eliminated |
| Log or receipt disclosure | Local storage and redaction are defaults; SQLite databases and WAL/SHM files use owner-only permissions | Anyone with access to the host account can read local state |

## Capability profiles

- `observe`: asks supported coding agents to use a read-only or planning mode.
- `workspace-write`: the default; permits writes in the dedicated task workspace under the external CLI sandbox.
- `trusted-command`: disables the supported CLI sandbox for one confirmed run. This is equivalent to trusting the model-influenced command with the permissions of the LightClaw host process.

External agent CLIs remain separate security products. Their own version, authentication, sandbox implementation, and configuration affect the real boundary.

## Security invariants

- An empty Telegram allowlist never means public access.
- Provider credentials and Telegram tokens are not copied into delegated process environments.
- In-memory approvals expire against wall and monotonic deadlines, so clock rollback cannot extend them and suspend time still consumes their lifetime.
- A task rollback never targets a directory without a matching LightClaw ownership record.
- Ownership registration preserves the requested task directory for its symlink check. A task
  symlink cannot register its target as LightClaw-owned or grant undo permission over that target.
- Existing configuration is backed up before a reset.
- Global Python and generic `~/.env` are not modified by the supported installer.
- Deterministic quality fixtures require no paid API key and no Telegram account.
- Active skills are valid prompt-guidance-only bundles whose reviewed instruction-and-manifest hash still matches; networked, writable, subprocess, or trusted-command declarations remain isolated from the core prompt.

Regression tests cover authorization, environment isolation/redaction, provider routing, traversal and symlink escape, chat file operations during symlink swaps, legacy skill migration limits, skill archive and permission boundaries, stale hash approval, Telegram multi-agent cancellation, process-tree termination, memory persistence, DAG dependency contracts, and scoped undo.

## Out of scope

The alpha does not claim protection against a compromised OS account, malicious code run through confirmed trusted execution, vulnerabilities in Telegram or provider infrastructure, physical access, or a user intentionally placing secrets in the task workspace. Containers or virtual machines remain the recommended extra boundary for high-risk tasks.
