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
| Unauthorized Telegram user | Numeric allowlist fails closed; an explicit public acknowledgement is required; privileged commands are rate-limited | A compromised allowed Telegram account remains authorized |
| Prompt injection | Human plan/privilege confirmation, restrictive capability profiles, bounded task workspaces | A model can still make harmful changes inside granted scope |
| Secret theft by delegated worker | Minimal environment allowlist; credential redaction in logs/results; no parent environment copy | Secrets already stored in readable workspace files remain visible to a worker |
| Workspace escape | Resolved non-symlink root, per-task direct child directory, external ownership record, external CLI sandbox flags | Trusted host execution intentionally removes this protection |
| Destructive rollback | Undo requires an ownership record, refuses symlinks/traversal, previews by default, and deletes only one task directory | Files copied elsewhere by trusted execution are outside the undo boundary |
| Orphan child process | New process group/session plus TERM/KILL tree handling on timeout/cancellation | OS or external CLI defects can still leave processes behind |
| Malicious skill archive | Archive size limits, encrypted-bundle rejection, normalized install boundary | Skill instructions are untrusted content and require review |
| Credential leakage in errors | Known-value, assignment, bearer, and Telegram-token redaction | Novel secret formats or secrets shorter than four characters may evade redaction |
| Dependency compromise | Pinned GitHub Actions, dependency audit, Dependabot, CodeQL, dependency review, Scorecard | Registry and maintainer compromise cannot be eliminated |
| Log or receipt disclosure | Local storage and redaction are defaults | Anyone with access to the host account can read local state |

## Capability profiles

- `observe`: asks supported coding agents to use a read-only or planning mode.
- `workspace-write`: the default; permits writes in the dedicated task workspace under the external CLI sandbox.
- `trusted-command`: disables the supported CLI sandbox for one confirmed run. This is equivalent to trusting the model-influenced command with the permissions of the LightClaw host process.

External agent CLIs remain separate security products. Their own version, authentication, sandbox implementation, and configuration affect the real boundary.

## Security invariants

- An empty Telegram allowlist never means public access.
- Provider credentials and Telegram tokens are not copied into delegated process environments.
- A task rollback never targets a directory without a matching LightClaw ownership record.
- Existing configuration is backed up before a reset.
- Global Python and generic `~/.env` are not modified by the supported installer.
- CI fixtures require no paid API key and no Telegram account.

Regression tests cover authorization, environment isolation/redaction, provider routing, traversal and symlink escape, skill archive boundaries, process-tree termination, memory persistence, DAG dependency contracts, and scoped undo.

## Out of scope

The alpha does not claim protection against a compromised OS account, malicious code run through confirmed trusted execution, vulnerabilities in Telegram or provider infrastructure, physical access, or a user intentionally placing secrets in the task workspace. Containers or virtual machines remain the recommended extra boundary for high-risk tasks.
