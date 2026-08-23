# Changelog

All notable LightClaw changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and releases use semantic versioning.

## [Unreleased]

### Added

- A privacy-safe private-alpha evidence contract, structured external tester issue form,
  aggregate generator, and derived release gates that reject identities, free text, stale
  counts, and manually inflated readiness.
- Versioned `v0.1.0` draft notes covering the full release contract, with CI enforcement
  that the eventual GitHub Release body is identical and contains no draft placeholders.
- A tag-driven atomic release flow: manual runs are rehearsal-only, PyPI and GHCR must
  succeed before GitHub creates the release from the committed notes and verified assets.
- Structured contribution forms, predictable support/security routes, an evidence-led PR
  standard, and one canonical local quality command.
- A privacy-gated public showcase with three token-free recipes, sanitized Run Cards,
  declared provenance, replay validation, and an explicit no-telemetry/no-upload contract.
- A validated evidence-led launch pack with one headline, a 24-second clip, five-minute
  quickstart, three recipes, security diagram, raw benchmarks, honest comparisons, and
  explicit not-started external launch status.
- Useful 100% GitHub community-profile files, a real docs URL, precise discovery topics,
  recurring release/Discussion update channels, and an evidence-only badge policy.
- A final roadmap evidence audit that separates completed repository implementation from
  11 still-unproven release and external-adoption gates.
- Token-free `lightclaw demo` stories for memory, repository work, and multi-agent orchestration.
- Private JSON/Markdown run receipts with checks, file hashes, artifacts, commands, and recovery context.
- Reproducible raw benchmark JSON/CSV output.
- Namespaced SQLite FTS5 lexical memory with bounded retention, private export, selective delete, and an optional embedding-rerank protocol.
- Permission-manifest skills with inactive-by-default installation, owner/version provenance, source-and-manifest hash review, isolated high-authority declarations, and a non-executing validator.
- One typed six-provider contract with normalized nullable usage, errors, bounded timeout/retry policy, explicit SDK close, recorded fixtures, and a generated compatibility matrix.
- Stable orchestration modules for routing, planning, worker tasks, acceptance, and execution; shared atomic JSON/file primitives; five accepted architecture records; and CI-enforced complexity/runtime budgets.

### Changed

- GitHub workflows use the current Node 24 action generations for checkout, Python setup,
  artifact handling and attestation, dependency review, and CodeQL while retaining
  immutable commit pins.
- Pin HTTPX 0.28.1 so the dependency graph records the installed safe version instead of
  treating an unresolved compatible range as potentially affected by a pre-0.23 advisory.
- Python 3.10 development checks use the `tomli` compatibility parser for architecture and
  runtime-footprint contracts.
- The canonical local quality command retries one transient cold-start measurement without
  changing the published runtime budget.
- Runtime-footprint unit tests use a deterministic timing sample; live timing remains an
  explicit quality, CI, and release measurement rather than a load-sensitive unit test.
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
