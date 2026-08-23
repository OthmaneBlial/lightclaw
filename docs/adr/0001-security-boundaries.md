# ADR-0001: Security Boundaries

- Status: Accepted
- Date: 2026-08-23

## Context

Telegram messages, hosted providers, skill instructions, local workspaces, and external coding-agent CLIs cross different trust boundaries. Treating the whole process as one sandbox would overstate protection.

## Decision

Telegram identity fails closed. Hosted providers receive only configured prompt context. Delegated CLIs receive a minimal environment and a dedicated owned workspace. `trusted-command` is a separately confirmed host boundary, not an extension of `workspace-write`. Prompt-only skills do not grant executable authority. Publication and destructive actions retain explicit confirmation.

## Consequences

The process still has the host permissions of the account running it, and trusted execution can escape the default workspace boundary. External SDKs and agent CLIs remain separate security products. Tests and documentation must describe which boundary was exercised instead of calling LightClaw generically “sandboxed.”
