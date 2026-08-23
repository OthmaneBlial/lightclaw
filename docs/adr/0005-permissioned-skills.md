# ADR-0005: Permissioned Skills

- Status: Accepted
- Date: 2026-08-23

## Context

Community instructions are untrusted content. Automatic activation, implicit dependencies, or a manifest that can change after approval would turn convenient sharing into an invisible authority escalation.

## Decision

Every skill has `SKILL.md` plus a versioned `skill.json` declaring capabilities, network domains, writable paths, dependencies, owner, and pinned version. Installations remain inactive and record provenance. Activation previews source and permissions and requires a token derived from both files. Only valid `prompt-guidance` skills enter the core prompt; network, writes, subprocesses, dependencies, and trusted commands remain isolated from core.

## Consequences

The manifest constrains LightClaw's loader but cannot certify instruction intent. Editing source or permissions invalidates approval. Executable extension support requires a future isolated runner and a separate ADR; it will not be added by installing packages into core.
