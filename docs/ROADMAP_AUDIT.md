# Roadmap Evidence Audit

**Audit date:** 2026-10-01
**Result:** repository-controlled implementation is complete; 11 live release/adoption
evidence gates remain open.

The unchecked items in [ROADMAP.md](../ROADMAP.md) are not missing code tasks. They require
publication to third-party registries or voluntary evidence from people outside the
maintainer-authored fixtures. Checking them without that evidence would violate the
roadmap's “proof over claims” rule.

## Open gates

| Roadmap gate | Current truthful state | Evidence required to close it |
|---|---|---|
| PyPI Trusted Publishing | OIDC/attestation workflow and protected `pypi` environment are configured; no `lightclaw-ai` version is discoverable on PyPI, and account-side trusted-publisher setup remains unverified | Live `lightclaw-ai` PyPI version, trusted-publisher provenance, and successful clean install |
| `v0.1.0` release | Release workflow, notes configuration, checklist, changelog, upgrade, and rollback docs exist; no tag/release exists | Published GitHub Release after its source commit has all required green checks |
| Complete release notes | The versioned `v0.1.0` file remains a draft; the active release workflow rejects draft markers and a published body that differs from the committed notes; no notes are published | Finalized versioned notes on the live release covering install, upgrade, uninstall, compatibility, limitations, security, provenance, and rollback |
| GHCR container | Release workflow can publish a versioned image and the systemd guide exists; no public image/tag/digest is verified in release evidence | Public image/tag/digest built from the release commit plus verified command/container smoke |
| 10 external installs / 9 demo successes | Privacy-bounded issue form, private report schema, and validated public aggregate exist; completed external reports: 0 | Sanitized aggregate with 10–20 attempts, at least 9 successes, versions, date window, and failures |
| Median deterministic success under 3 minutes | The aggregate derives the gate and rejects missing/hand-edited evidence; external timing samples: 0 | Median from at least 9 successful timings in the same external cohort, with denominator and missing/failed attempts |
| Median real Telegram task under 10 minutes | The aggregate derives the gate and reports missing values; external real-device timing samples: 0 | Sanitized real Telegram timing aggregate with denominator and failure handling |
| Verified `v0.1.0` artifacts | No release exists | Wheel/sdist, attestations, runtime footprint, notes, and rollback links on the live release |
| Five external bounded repo tasks | Maintainer fixtures are green; external completions: 0 | Five consented reports showing a real bounded task and user explanation of the change |
| Three voluntary public Run Cards | Three maintainer fixture cards exist; voluntary external cards: 0 | Three privacy-reviewed community entries with provenance and publication consent |
| One community workflow per release | `showcase/featured.json` is intentionally `null` | A consented external entry selected for each applicable release |

## Implemented machinery behind those gates

- Trusted PyPI OIDC, build provenance attestations, GitHub Release assets, and GHCR
  publication are defined in the release workflow.
- The protected GitHub `pypi` environment requires a deliberate reviewer approval; no
  long-lived registry token is stored in the repository.
- `lightclaw demo` and all three showcase recipes are token-free and replayed by the canonical local quality suite and release workflow; the separate showcase validation workflow is disabled.
- The [alpha evidence contract](../launch/alpha/) accepts only consented external reports,
  rejects identifier/free-text fields, keeps raw evidence out of Git, publishes failures
  and missing values, and derives the cohort/time gates from the aggregate.
- The [versioned release notes](releases/v0.1.0.md) are an explicit draft; the release
  workflow refuses draft markers and a GitHub body that differs from the committed file.
- The [release checklist](../launch/RELEASE_CHECKLIST.md) refuses promotion when security,
  compatibility, rollback, alpha, or community evidence is absent.
- `showcase/featured.json` and `--require-community-feature` make the community gap
  machine-visible instead of silently substituting maintainer fixtures.
- [launch/status.json](../launch/status.json) records external stages as not started/not
  released and requires evidence URLs before a completed state is valid.

## Current reproducible repository evidence

- Canonical local quality command: lint, provider matrix, architecture/runtime budgets,
  skill contract, showcase privacy/replay, alpha aggregate, versioned release notes,
  launch-pack validation, full tests, and package build.
- Earlier compatibility matrix on 2026-09-30: all 400 tests passed on macOS arm64 with
  Python 3.10.21, 3.11.16, 3.12.14, 3.13.1, and 3.14.7. Python 3.10–3.13 emitted three
  third-party deprecation warnings each; Python 3.14 emitted four from `python-telegram-bot`
  and `google-genai`.
- Python 3.10 canonical quality at `c5fa6f4` on 2026-10-01: all 847 tests passed on macOS
  arm64 with Python 3.10.21; lint, documentation links, provider matrix, architecture/runtime
  budgets, locked dependency resolution, runtime footprint, skill validation, showcase replay,
  launch evidence, dependency audit, package build, and clean-wheel installation also passed.
  The three legacy skill migration cases cover symlink files, symlink swaps, and oversized files.
  The clean venv used the pinned pip from `requirements-pip.txt`; Python 3.10.21's bundled pip
  23.0.1 could not resolve one hashed transitive extra.
- Previous cross-version canonical suite at `89b4ec4` on 2026-10-01: all 860 tests passed on
  macOS arm64 with Python 3.12.5, 3.13.1, and 3.14.7; lint, documentation links, architecture/runtime
  budgets, dependency audit, package build, and clean-wheel installation passed on all three versions.
- Pagination commit `007e41c` passed the full local `scripts/quality.py` gate on macOS arm64 Python
  3.14.7: 863 tests, lint, documentation links, provider artifacts, architecture/runtime budgets,
  dependency audit, sdist/wheel build, and clean-wheel installation.
- History-index follow-up `baec299` passed the same full gate on Python 3.14.7: 864 tests and the
  complete package checks. Its query-plan regression confirms page reads use the composite index
  without a temporary sort. Python 3.10 is unavailable; its last full run is recorded above.
  Tests cover immediate local cancellation before delayed SQLite persistence, clear reporting when
  persistence fails, visible cancel controls during preflight and queued/waiting/repair states,
  and workspace cleanup when preflight fails. Telegram `/agent runs` lists ten durable jobs per
  page in the current chat with escaped bounded goal previews, per-lane state counts, and chat- and
  page-scoped buttons for opening completed-run diffs after restart; the session filter is applied
  in SQLite with a matching session-history index, and receipt reads are pinned below the configured
  workspace. Agent timeout cleanup gives output
  streams a 250ms drain window, then cancels readers instead of waiting for detached descendants
  holding inherited pipes. Synchronous doctor probes also cap post-timeout pipe draining, close
  inherited pipes held by detached descendants, preserve partial output, and reap the probe.
  Git and GitHub artifact commands use the same bounded post-timeout pipe cleanup and preserve
  partial output when detached descendants outlive their process group.
  Delegated agent shutdown bounds `Process.wait()` and aborts buffered stdin when detached
  descendants retain inherited pipes, covering Python 3.13's subprocess wait behavior.
  Cron help shows local date/time, Unix seconds, and explicit-offset forms separately; nonexistent
  local times are rejected without falling back to date-only midnight, and numeric local UTC offsets
  appear in schedule displays. One third-party
  `google-genai` deprecation warning remains.
- Stable-history snapshot follow-up passed the full local `scripts/quality.py` gate on macOS arm64
  Python 3.14.7: 867 tests, lint, documentation links, provider artifacts, architecture/runtime
  budgets, dependency audit, sdist/wheel build, and clean-wheel installation. Run-history callbacks
  carry a per-chat SQLite rowid watermark, so newer jobs cannot shift open pages or invalidate their
  diff buttons; reopening `/agent runs` includes new jobs. Cross-session snapshots remain separate,
  out-of-range callback values are rejected, and page reads still use the session-history index
  without a temporary sort. The runtime-line cap is 22,125 for this 71-line change, documented in
  `docs/architecture/core-budget.json`.
- Latest canonical `scripts/quality.py` run at `5792f1b` passed on macOS arm64 Python 3.14.7:
  871 tests, lint, documentation links, provider artifacts, architecture/runtime budgets, locked
  dependency resolution, dependency audit, sdist/wheel build, and clean-wheel installation. The
  cross-chat history regression covers legacy and snapshot-cursor callbacks; new file-path tests
  protect `.git`, `.lightclaw`, and `.lightclaw-meta` from model context and file-block edits.
  Recent-file discovery also prunes those private directories plus generated dependency, build, and
  cache trees while preserving explicitly named and last-used paths.
- Latest canonical `scripts/quality.py` run at `943e2d8` passed on macOS arm64 Python 3.14.7:
  872 tests, lint, documentation links, provider artifacts, architecture/runtime budgets, locked
  dependency resolution, dependency audit, sdist/wheel build, and clean-wheel installation. A new
  regression reproduces and fixes cross-user memory recall when concurrent Telegram group updates
  bind scopes through worker threads: each update now activates its user/workspace scope in its own
  async context while SQLite persistence remains off the event loop. One third-party
  `google-genai` deprecation warning remains.
- Latest canonical `scripts/quality.py` run at `3da9e25` passed on macOS arm64 Python 3.14.7:
  873 tests and all local quality, audit, package, and clean-wheel checks. Multi-agent receipts now
  preserve terminal acceptance and dependency failure reasons; the regression covers a failed
  acceptance after a repair attempt. Receipt assembly also keeps the main orchestrator at 694 lines,
  within the documented 700-line function ceiling; the runtime-line cap is 22,179. One third-party
  `google-genai` deprecation warning remains.
- Latest canonical `scripts/quality.py` run for `254820a` passed on macOS arm64 Python 3.14.7:
  890 tests, all local quality and audit checks, sdist/wheel build, and clean-wheel installation. A
  privacy regression now restricts public-group memory recall, interaction counts, and heartbeat context to
  that group session; private chats retain cross-chat recall. Runtime-line cap: 22,213. The same
  third-party `google-genai` deprecation warning remains.
- Latest canonical `scripts/quality.py` run for `3a8bf5b` passed on macOS arm64 Python 3.14.7:
  897 tests and all local quality, audit, packaging, and clean-wheel checks. Result acceptance now
  verifies the exact reviewed patch, branch, and checkpoint, refuses post-review edits, and safely
  retries an already-created matching commit if durable job-state recording failed. Git content
  filters remain disabled during verification. Runtime-line cap: 22,323; the existing third-party
  `google-genai` deprecation warning remains.
- Telegram forum-topic sessions passed the full local `scripts/quality.py` gate on macOS arm64
  Python 3.14.7: 923 tests, lint, documentation, architecture, dependency, showcase, packaging, and
  clean-wheel checks. Session state, approvals, work serialization, and memory are topic-scoped;
  cron, heartbeat, and typing updates target the originating topic. Runtime-line cap: 22,364; one
  existing third-party `google-genai` deprecation warning remains.
- GitHub CI, CodeQL, OpenSSF Scorecard, and showcase validation are disabled, so no GitHub CI
  run exists for this local validation.
- The release workflow's manual rehearsal succeeded at commit `ee7c49f` on 2026-08-24
  ([run](https://github.com/OthmaneBlial/lightclaw/actions/runs/32774972863)); PyPI, GHCR,
  and GitHub Release publication jobs remained skipped by contract.
- A local Linux container build from the preceding alpha-evidence commit succeeded under
  an unprivileged user; `--read-only` plus temporary filesystems completed the deterministic
  memory demo. This is pre-release smoke evidence, not a substitute for a public GHCR digest.
- The current `python:3.14-slim` digest matches the official registry tag, and local quality
  passes on Python 3.14.7. A container build with this Dockerfile remains unverified because
  the existing Podman VM cannot start without its configured SSH identity file.
- GitHub controls rechecked on 2026-10-01: CI, CodeQL, OpenSSF Scorecard, and showcase workflows
  are `disabled_manually`; Release, Dependabot Updates, and Dependency Graph remain active; no
  open issues exist. Repository description and topics are populated. The project Pages endpoint
  is served from `OthmaneBlial/OthmaneBlial.github.io` on `master`, and the live LightClaw URL
  returned HTTP 200.

Live state can change after this snapshot. Release and external-adoption gates must be
rechecked at the time they are claimed; this document is not a substitute for their URLs.
