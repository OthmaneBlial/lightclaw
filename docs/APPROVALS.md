# Telegram Approval Contract

LightClaw treats a Telegram request and an execution approval as separate events.

## Plan review

Before a multi-agent run, the bot shows:

- declared changed paths and acceptance commands;
- the capability profile and computed risk level;
- a bounded duration range based on lane count;
- an explicit notice when cost is unavailable from the local CLI before execution;
- compact actions for Approve, Edit scope, Deny, and Cancel.

If scope is missing, the review says so rather than inventing paths. Editing a scope regenerates the plan and requires a new approval.

## High-risk confirmation

Destructive language, publishing/deployment, credential changes, or external-system scope requires two ordered confirmations. A forged second-confirmation callback is rejected unless the first approval was recorded. Trusted host execution retains its separate confirmation gate.

## Voice goals

Voice input is transcribed, displayed as “not executed,” and retained in memory only as a short-lived pending action. Nothing enters the normal agent loop until the user taps Use transcription. Discard and expiry execute nothing.

## Result controls

Completed runs show View diff, Retry failed lane when applicable, Accept result, and Cancel. Retry still obeys durable idempotency and attempt bounds; unsafe retries fail closed. Accept changes only the local durable disposition and never pushes or publishes.

Responses larger than 6,000 characters or detected as large code dumps are written to an owner-only Markdown artifact and attached to Telegram. LightClaw avoids splitting them into unreadable chat walls.
