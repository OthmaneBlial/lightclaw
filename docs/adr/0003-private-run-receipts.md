# ADR-0003: Private Run Receipts

- Status: Accepted
- Date: 2026-08-23

## Context

Users need proof of what an approved run attempted, changed, checked, and failed. Raw prompts, repository paths, command output, and credentials are unsafe as automatic telemetry or public artifacts.

## Decision

Write versioned JSON and Markdown receipts locally with owner-only permissions. Record approved goal/scope, capability, plan, nullable usage, redacted commands, content-addressed file changes, checks, handoffs, failures, retries, checkpoint, disposition, and undo. Exported share cards are a separate bounded projection and require an explicit local command. Never auto-upload either form.

## Consequences

A receipt proves only what LightClaw observed; unavailable tokens or cost stay null. Host-account access can still reveal local receipts. Schema changes require validation tests and backward-readable evidence or an explicit version transition.
