# Privacy and Public Evidence

LightClaw has no product analytics, telemetry endpoint, hosted control plane, or automatic
showcase uploader. Runtime data stays under the configured local home unless the operator
explicitly sends a prompt to a chosen provider or performs a separate publication action.

## Data and network boundaries

| Data | Default location or recipient | Automatic public upload |
|---|---|---|
| Config and credentials | owner-readable app config on the host | never |
| Memory, skills, logs, jobs, receipts | local `.lightclaw/` state | never |
| Task source and artifacts | local owned workspace | never |
| Long Telegram responses | `<workspace>/.lightclaw-meta/messages/response-*.md`; retained until manually deleted | no public upload; when available, attached to the current Telegram chat |
| Telegram messages | Telegram Bot API and the local process | never by LightClaw |
| Configured model context | the explicitly selected provider | not public, but leaves the host |
| Delegated task context | the explicitly selected local coding-agent CLI | governed by that CLI |
| Sanitized Run Card | explicit local destination after `run export --apply` | never |
| Showcase entry | manually reviewed Git commit/pull request | only by that explicit action |

Optional JSONL logs use owner-only files. LightClaw rotates them at 5 MiB and retains up to
three backups; message, exception, and session fields are capped at 4,096 characters. LightClaw
redacts known environment secrets and recognizable credential patterns before console or JSONL
handlers write records. Detection is heuristic. LightClaw rejects a symlink at the configured
log file path.

Responses too large for inline Telegram messages, and responses that look like code dumps, are saved under the configured workspace's `.lightclaw-meta/messages/` directory before attachment. LightClaw does not automatically prune these files; delete old `response-*.md` files manually when no longer needed.

During an edit requested in `/mode edit`, LightClaw may send bounded workspace file excerpts to the configured provider for a retry or repair pass. Common credential paths and file contents matching current secret-detection patterns are omitted; detection is heuristic.

Private receipts may contain goals, commands, workspace-relative details, recovery context,
and timestamps. They are mode `0600` and are not suitable for direct publication. The Run
Card exporter uses an allowlist and omits commands, handoffs, checkpoints, undo paths, and
timestamps, but its output still requires human review.

The public [showcase](../showcase/) accepts only synthetic or explicitly publishable input,
declared provenance and consent, a sanitized Run Card, and a token-free reproducible recipe
or permission-manifest skill. `scripts/quality.py` checks known secret/path patterns and
replays recipes locally. The corresponding GitHub Showcase privacy and reproducibility
workflow is disabled. No automated sanitizer can prove anonymity, so a maintainer also
reviews the full contribution.

The checked-in GitHub Actions artifact rules restrict uploads to package distributions,
coverage, security results, and generated benchmark/runtime evidence. A regression test
rejects workflow artifact blocks that mention receipts, prompts, runtime homes, workspaces,
or repositories.
