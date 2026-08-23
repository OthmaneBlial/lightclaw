# Changelog

All notable LightClaw changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and releases use semantic versioning.

## [Unreleased]

### Added

- Structured contribution forms, predictable support/security routes, an evidence-led PR
  standard, and one canonical local quality command.
- A privacy-gated public showcase with three token-free recipes, sanitized Run Cards,
  declared provenance, replay validation, and an explicit no-telemetry/no-upload contract.
- A validated evidence-led launch pack with one headline, a 24-second clip, five-minute
  quickstart, three recipes, security diagram, raw benchmarks, honest comparisons, and
  explicit not-started external launch status.
- Token-free `lightclaw demo` stories for memory, repository work, and multi-agent orchestration.
- Private JSON/Markdown run receipts with checks, file hashes, artifacts, commands, and recovery context.
- Reproducible raw benchmark JSON/CSV output.
- Namespaced SQLite FTS5 lexical memory with bounded retention, private export, selective delete, and an optional embedding-rerank protocol.
- Permission-manifest skills with inactive-by-default installation, owner/version provenance, source-and-manifest hash review, isolated high-authority declarations, and a non-executing validator.
- One typed six-provider contract with normalized nullable usage, errors, bounded timeout/retry policy, explicit SDK close, recorded fixtures, and a generated compatibility matrix.
- Stable orchestration modules for routing, planning, worker tasks, acceptance, and execution; shared atomic JSON/file primitives; five accepted architecture records; and CI-enforced complexity/runtime budgets.

### Changed

- Python 3.10 development checks use the `tomli` compatibility parser for architecture and
  runtime-footprint contracts.
- Migrated Gemini from deprecated `google-generativeai` to `google-genai`; updated supported OpenAI and Anthropic SDK major ranges behind the provider protocol.
- Release evidence now includes cold-start samples, direct dependency count, and wheel size as a separate machine-readable artifact.

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

- Memory is bounded lexical FTS5 retrieval by default, not a claim of semantic understanding.
- External agent sandboxes, hosted providers, and trusted host execution remain separate trust boundaries.
- Fixture demos do not prove real model quality or Telegram delivery.

[Unreleased]: https://github.com/OthmaneBlial/lightclaw/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/OthmaneBlial/lightclaw/releases/tag/v0.1.0
