# Changelog

All notable LightClaw changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and releases use semantic versioning.

## [Unreleased]

### Added

- Token-free `lightclaw demo` stories for memory, repository work, and multi-agent orchestration.
- Private JSON/Markdown run receipts with checks, file hashes, artifacts, commands, and recovery context.
- Reproducible raw benchmark JSON/CSV output.

## [0.1.0] - Unreleased

### Added

- Telegram and terminal control surfaces with six provider routes.
- Codex and Claude single/multi-agent delegation with DAG planning, approval, handoffs, acceptance checks, and bounded repair.
- Persistent local SQLite memory, skills, voice transcription, heartbeat, and scheduled jobs.
- Standard `lightclaw-ai` packaging, isolated installation, app-specific private configuration, doctor, undo, and uninstall commands.
- Python 3.10–3.13 CI across Ubuntu and macOS, package smoke tests, dependency audit, CodeQL, secret scanning, Dependabot, and OpenSSF Scorecard.

### Security

- Telegram access fails closed; public access requires an explicit acknowledgement.
- Delegated workers receive a minimal environment and restrictive capability profile by default.
- Task workspaces have external ownership records, starting checkpoints, process-tree cancellation, and scoped rollback.
- Runtime logs omit prompt and Telegram content by default.

### Known limitations

- Memory is local lexical-vector retrieval with a full SQLite scan, not an embedding service or vector database.
- External agent sandboxes, hosted providers, and trusted host execution remain separate trust boundaries.
- Fixture demos do not prove real model quality or Telegram delivery.

[Unreleased]: https://github.com/OthmaneBlial/lightclaw/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/OthmaneBlial/lightclaw/releases/tag/v0.1.0
