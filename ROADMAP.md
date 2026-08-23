# LightClaw Roadmap

> A path from a promising repository to a trusted, memorable, and highly shareable open-source product.

**Roadmap date:** 2026-08-23

**Status:** implementation in progress; checkboxes reflect verified repository or live-platform evidence

**Rule:** stars are a lagging signal. The roadmap optimizes first for successful installs, visible proof, trust, repeat use, and external contributors.

## Executive decision

LightClaw should **not** try to become a smaller feature-for-feature copy of OpenClaw.

“Lightweight OpenClaw in Python” is already a crowded position. Projects such as nanobot already combine Python, a small core, memory, tools, multiple channels, and a much larger community. Chasing their feature list would make LightClaw less light without making it more memorable.

LightClaw should instead own this position:

> **The auditable Telegram mission control for local AI coding agents.**
>
> Send a goal from your pocket, review the plan, watch the work, and receive a verified result.

The signature loop should be unmistakable:

```text
Telegram text or voice goal
  -> scoped DAG plan with risk and expected outputs
  -> approve, edit, or cancel
  -> isolated Codex/Claude workers
  -> live compact progress
  -> diff + tests + artifacts + cost/time receipt
  -> accept, undo, export, or open a pull request
```

LightClaw already contains much of this engine. The opportunity is to make it safe, reliable, easy to install, and impossible to misunderstand in the first 30 seconds.

## Audited baseline

### What is already strong

- A focused Telegram-first interface plus local terminal chat.
- Six LLM provider paths.
- Local SQLite memory, skills, file operations, voice transcription, heartbeat, and scheduled jobs.
- Codex and Claude delegation with progress reporting.
- Multi-agent planning with confirmation, DAG scheduling, owned paths, machine-readable handoffs, acceptance checks, repair passes, and cross-lane audits.
- A clear preference for a small, understandable Python system over a broad platform.

### What currently blocks breakout adoption

| Area | Current evidence | Why it matters |
|---|---|---|
| Positioning | The headline leads with “lightweight OpenClaw alternative” | The claim is crowded and does not expose LightClaw's strongest workflow: Telegram-to-verified-work delegation. |
| Safety | An empty `TELEGRAM_ALLOWED_USERS` allows everyone; delegated agents receive broad CLI permissions; `LOCAL_AGENT_SAFETY_MODE` defaults to `off` | Promotion would amplify a dangerous default around a system that can edit files and execute local agents. |
| Secret isolation | Delegated processes inherit the parent environment | Provider keys, Telegram tokens, and unrelated host secrets must not be available to workers by default. |
| Installation | `setup.sh` installs into the global Python environment, writes `~/.env`, and uses GNU-only `grep -P`; the documented raw installer comment points to `master` while the default branch is `main` | The claimed one-command path is fragile on macOS and can collide with unrelated user configuration. |
| Packaging | No `pyproject.toml`, wheel, sdist, `pipx`/`uv` path, or package provenance | A developer tool needs a reversible, standard installation path. The `lightclaw` name on PyPI is already used by an unrelated package. |
| Quality proof | No automated test suite and no GitHub Actions workflow | Users cannot independently verify security, provider contracts, memory behavior, or orchestration correctness. |
| Release proof | No GitHub releases or changelog | There is no stable version, upgrade story, release channel, or downloadable milestone. |
| Product proof | The repository has one logo, no screenshot, video, fixture demo, example gallery, or reproducible benchmark | Visitors must imagine the value instead of seeing it. |
| Claims | “Infinite memory” and “semantic recall” are currently implemented as in-process lexical vectors with a full SQLite scan | The language overstates the implementation and will damage trust when technical users inspect it. |
| Complexity | Roughly 10.9k lines of Python; the largest orchestration modules exceed 1,400 and 1,800 lines | “Tiny codebase” needs a measured definition and explicit complexity budgets. |
| Community | 20 stars, 2 forks, no Discussions, and a 57% GitHub community profile at the audit snapshot | Contributors lack structured issue intake, security reporting, support routing, and bounded starter work. |

## Product principles

Every roadmap item must reinforce at least one of these principles:

1. **Safe by default.** A new user should not need to discover the secure configuration after installation.
2. **Proof over claims.** Every reliability, memory, compatibility, and performance statement must link to a reproducible check.
3. **Telegram is the product surface.** It is not merely one adapter in a long channel list.
4. **Human approval at meaningful boundaries.** Plans, dangerous commands, external publication, and destructive changes stay reviewable.
5. **Local-first, not falsely local.** Describe local memory/workspaces precisely when models still use hosted APIs.
6. **A small core with optional edges.** Examples, adapters, and community skills should not continuously inflate the runtime.
7. **No silent telemetry.** Growth measurement remains repository-side or explicitly opt-in.

## Phase 0 — Trustworthy alpha

**Target:** weeks 1–2

**Goal:** make it responsible to invite strangers to install LightClaw.

This is a launch blocker. Do not run a major promotion before the exit criteria pass.

### P0.1 Fail closed on Telegram access

- [x] Replace “empty allowlist means public” with first-owner pairing or a mandatory allowlist.
- [x] Require an explicit, alarming `LIGHTCLAW_PUBLIC_BOT_ACK=yes` override for intentionally public bots.
- [x] Show the active access policy in `lightclaw doctor`, `/show`, and startup logs without exposing IDs unnecessarily.
- [x] Test every command and media handler against unauthorized users.
- [x] Rate-limit pairing and privileged commands. (The mandatory-allowlist design has no remote pairing endpoint.)

### P0.2 Isolate secrets and authority

- [x] Replace `os.environ.copy()` for delegated workers with a minimal environment allowlist.
- [x] Prove that provider keys, Telegram tokens, unrelated shell secrets, and `.env` contents cannot appear in worker prompts, logs, handoffs, or run receipts.
- [x] Add capability profiles: `observe`, `workspace-write`, and `trusted-command`.
- [x] Default delegation safety to the restrictive profile.
- [x] Require a per-run confirmation for privilege escalation.
- [x] Treat regex deny patterns as defense in depth, never as the security boundary.

### P0.3 Contain and recover delegated work

- [x] Run coding tasks in a dedicated Git worktree, temporary checkout, or optional container.
- [x] Snapshot the starting commit and dirty state before execution.
- [x] Add `cancel`, timeout, and process-tree termination that are tested on Linux and macOS.
- [x] Add an undo path that reverts only LightClaw-owned changes and never discards pre-existing user work.
- [x] Refuse to operate on ambiguous workspace roots or unresolved symlink paths.

### P0.4 Replace the fragile installer path

- [x] Add `pyproject.toml` with a conventional importable package, typed project metadata, and a `lightclaw` console entry point.
- [x] Use a distinct distribution name such as `lightclaw-ai` after rechecking availability; keep the command name `lightclaw`.
- [x] Support `uv tool install lightclaw-ai` and `pipx install lightclaw-ai`.
- [x] Move configuration from generic `~/.env` to an app-specific config path and runtime data to an app-specific data path.
- [x] Create secrets with mode `0600` and document every path touched.
- [x] Never overwrite an existing configuration without an explicit backup and confirmation.
- [x] Fix the `grep -P` macOS failure and stale `master` installer reference while the legacy script remains supported.
- [x] Add `lightclaw uninstall --dry-run` and documented manual rollback.

### P0.5 Establish a real quality gate

- [x] Add `pytest` unit and integration suites.
- [x] Start with the highest-risk contracts: authorization, environment redaction, traversal/symlink escape, skill install boundaries, destructive actions, cancellation, memory persistence, provider routing, and DAG dependency scheduling.
- [x] Add deterministic fake-provider, fake-Telegram, and fake-agent adapters. CI must not require paid keys.
- [x] Add a GitHub Actions matrix for Python 3.10–3.13 on Ubuntu and macOS.
- [x] Run tests, formatting/linting, package build/install smoke tests, and dependency auditing.
- [x] Pin third-party Actions to full commit SHAs with read-only permissions by default.
- [x] Protect `main` with required green checks, resolved conversations, and blocked force-push/deletion.

### P0.6 Publish the security contract

- [x] Add `SECURITY.md`, supported-version policy, private vulnerability reporting, and a response target.
- [x] Add a short threat model covering Telegram identity, prompt injection, skills, workspace writes, delegated CLIs, secrets, logs, and the host boundary.
- [x] Enable Dependabot, CodeQL, secret scanning where available, and OpenSSF Scorecard after the basic controls are real.
- [x] Document what LightClaw cannot protect when a user explicitly enables trusted host execution.

### Phase 0 exit criteria

- [x] A clean macOS and Ubuntu install completes without editing global Python or a generic home `.env`.
- [x] Unauthorized Telegram users fail every privileged integration test.
- [x] Delegated child processes receive zero unrelated secrets in a regression fixture.
- [x] Cancellation kills the worker tree and preserves pre-existing user changes.
- [x] CI is green on all supported Python/OS combinations.
- [x] No unresolved critical or high-severity dependency/security finding.
- [x] `SECURITY.md`, threat model, install, upgrade, and uninstall paths are public.

## Phase 1 — The five-minute wow

**Target:** weeks 3–5

**Goal:** let a visitor understand LightClaw in 10 seconds and reproduce one success in five minutes.

### P1.1 Rebuild the README around one result

- [x] Lead with: **“Turn a Telegram request into reviewed, verified work on your local projects.”**
- [x] Put a silent, captioned 20–30 second Telegram demo directly below the headline.
- [x] Show one complete story: request, DAG plan, approval, parallel work, checks, and final diff/artifact.
- [x] Put `Watch the demo` and `Install safely` before the feature inventory.
- [x] Add a concise “LightClaw is / is not” table.
- [x] Move provider matrices, command catalogs, and deep orchestration details into docs.

### P1.2 Make success deterministic

- [x] Add `lightclaw demo` using recorded/fake adapters, with no token, paid API, or Telegram account required.
- [x] Finish with an observable artifact and receipt, not merely “process started.”
- [x] Add `lightclaw doctor --json` for support and CI, with aggressive secret redaction.
- [x] Keep the first-run happy path to five commands or fewer.

### P1.3 Ship three reproducible stories

- [x] `examples/telegram-memory`: store, restart, and recall a known fact.
- [x] `examples/telegram-repo-task`: modify a bounded fixture repo and return a diff plus tests.
- [x] `examples/telegram-multi-agent`: approve a two-lane plan and produce handoffs plus a final audit.
- [x] Include prerequisites, exact prompt, expected output, duration/cost range, cleanup, and security limits in each example.
- [ ] Run fixture mode for every example in CI; document a separate real-device/manual verification.

### P1.4 Measure “lightweight” honestly

- [x] Add a reproducible `bench/` suite with raw JSON/CSV output.
- [x] Measure clean install time, dependency count, idle RAM, startup time, routing overhead, core LOC, and minimum tested VPS/container size.
- [x] Add memory retrieval quality on a versioned fixture corpus.
- [x] Add a deterministic orchestration scenario measuring dependency order, handoff completion, failure reporting, and repair behavior.
- [x] Publish commit, OS, Python, hardware, run count, and whether each result is mocked or live.
- [x] Replace “tiny” and “infinite” with precise numbers until the stronger claims are earned.

### P1.5 Cut the first credible release

- [x] Add `CHANGELOG.md` and an upgrade/migration policy.
- [x] Build and verify wheel and sdist in clean environments.
- [ ] Publish the distinct package distribution through PyPI Trusted Publishing with attestations.
- [ ] Release `v0.1.0` only when Phase 0 and the deterministic demo are green.
- [ ] Include install, upgrade, uninstall, compatibility, known limitations, and security boundaries in release notes.
- [ ] Publish a GHCR container and a minimal systemd guide as optional deployment paths, not new core architecture.

### Phase 1 exit criteria

- [ ] At least 10 external testers attempt a fresh install; at least 9 complete the deterministic demo.
- [ ] Median deterministic time-to-first-success is under 3 minutes.
- [ ] Median real Telegram time-to-first-task is under 10 minutes.
- [ ] Three examples pass in CI and match their documented outputs.
- [x] Every performance claim links to raw reproducible evidence.
- [ ] `v0.1.0` has verified artifacts, provenance, release notes, and rollback instructions.

## Phase 2 — Build the signature product loop

**Target:** weeks 6–10

**Goal:** make Telegram-to-verified-work meaningfully better than using a generic chat adapter.

### P2.1 Introduce the LightClaw Run Receipt

Every run should produce a local Markdown and JSON receipt containing:

- [ ] original goal and approved scope;
- [ ] risk level and granted capability profile;
- [ ] plan/DAG and worker/model assignment;
- [ ] start/end times and bounded token/cost estimate where providers expose usage;
- [ ] commands run, exit status, and redacted output summary;
- [ ] files created/changed/deleted and a compact diff summary;
- [ ] tests/checks requested and actual evidence;
- [ ] handoffs, artifacts, failures, retries, and final disposition;
- [ ] starting checkpoint and safe undo instructions.

Receipts must be local and private by default. `lightclaw run export` may generate a sanitized share card only after showing exactly what will be included.

### P2.2 Make approvals first-class in Telegram

- [ ] Add compact inline actions for approve, edit scope, deny, cancel, retry failed lane, view diff, and accept result.
- [ ] Show changed paths, proposed commands, risk level, and estimated cost/time before approval.
- [ ] Require a second confirmation for destructive operations, publishing, credential changes, or writes outside the normal workspace.
- [ ] Render long results as files/artifacts instead of unreadable Telegram walls of text.
- [ ] Make voice goals first-class but always show the transcription before executing.

### P2.3 Add durable job control

- [ ] Persist run state in SQLite so restart does not erase what happened.
- [ ] Support one active writer per workspace, a visible queue, priorities, cancel, resume, and bounded retry.
- [ ] Make every lane idempotent or explicitly non-resumable.
- [ ] Detect overlapping owned paths before parallel execution.
- [ ] Surface stalled/abandoned processes through `lightclaw doctor` and Telegram status.

### P2.4 Deliver a real code artifact

- [ ] Produce a clean patch or branch from the isolated worktree.
- [ ] Support `accept`, `reject`, and `apply selected files` locally.
- [ ] Add optional pull-request creation through the authenticated `gh` CLI, with a full preview and explicit confirmation.
- [ ] Never push, open a PR, or publish externally by default.
- [ ] Attach the run receipt and test evidence to the proposed PR body.

### Phase 2 exit criteria

- [ ] A single demo shows phone request to verified patch/PR preview without hidden manual steps.
- [ ] 100 repeated fixture runs produce valid receipts with no secret leakage.
- [ ] Crash/restart/cancel/undo tests preserve unrelated user work.
- [ ] Five external users complete a real bounded repo task and can explain what LightClaw changed.
- [ ] At least three sanitized Run Cards are voluntarily submitted to the public showcase.

## Phase 3 — Make the core defensible

**Target:** weeks 11–16

**Goal:** improve memory, skills, provider support, and maintainability without turning LightClaw into a platform.

### P3.1 Make memory truthful and useful

- [ ] Rename the current implementation “lexical recall” until semantic behavior is measured.
- [ ] Add per-user and per-workspace namespaces so unrelated sessions cannot contaminate recall.
- [ ] Add retention controls, export, selective delete, and a clear local data map.
- [ ] Use SQLite FTS5 as the lightweight baseline; make embeddings an optional adapter.
- [ ] Add a hybrid retrieval evaluation corpus and publish precision/recall tradeoffs.
- [ ] Bound database growth and query latency; remove “infinite” from product copy.

### P3.2 Turn skills into a permissioned extension layer

- [ ] Add a minimal skill manifest: required capabilities, network access, writable paths, dependencies, and version.
- [ ] Preview source, permissions, owner, version, and content hash before activation.
- [ ] Pin installed versions and record provenance.
- [ ] Add a validation command and CI contract for community skills.
- [ ] Keep executable dependencies out of the core and isolate networked/high-authority skills.
- [ ] Publish a “build a safe skill in 10 minutes” tutorial.

### P3.3 Stabilize providers without multiplying branches

- [ ] Define one typed provider protocol with normalized text, usage, retry, timeout, and error behavior.
- [ ] Contract-test all six adapters with recorded fixtures.
- [ ] Publish a CI-generated compatibility matrix rather than a manually asserted support table.
- [ ] Audit provider SDK lifecycle and migrate deprecated libraries behind the protocol.
- [ ] Allow new providers only when a maintainer and contract tests exist.

### P3.4 Pay down orchestration complexity

- [ ] Split the largest modules by stable domain boundaries, not arbitrary line counts.
- [ ] Set a core complexity budget and review net-new code against it.
- [ ] Replace duplicated file/state helpers with tested small primitives.
- [ ] Add architecture decision records for security boundary, job persistence, receipts, memory, and skill permissions.
- [ ] Measure cold start and dependency growth on every release.

### Phase 3 exit criteria

- [ ] Memory claims are backed by a public versioned evaluation.
- [ ] Every active hub skill has recorded provenance and declared permissions.
- [ ] Six provider adapters pass the same contract suite.
- [ ] No single module owns planning, execution, persistence, rendering, and acceptance at once.
- [ ] Runtime footprint remains within an explicitly published budget.

## Phase 4 — Community and compounding distribution

**Target:** months 4–6

**Goal:** turn users' successful workflows into the next user's reason to try LightClaw.

### P4.1 Make contribution predictable

- [ ] Add issue forms for reproducible bugs, bounded proposals, provider compatibility, and showcase submissions.
- [ ] Route vulnerabilities to private reporting and support questions to Discussions.
- [ ] Add a PR template requiring before/after behavior, tests, risk note, compatibility impact, and docs updates.
- [ ] Add `CODE_OF_CONDUCT.md`, `SUPPORT.md`, a module map, and one canonical local quality command.
- [ ] Reframe “No Vibe-Coded PRs” as an evidence standard: AI-assisted work is welcome when the contributor understands, tests, and owns it.
- [ ] Seed 8–12 real `good first issue` tasks with acceptance criteria; do not manufacture feature work.

### P4.2 Build a privacy-respecting showcase loop

```text
use a workflow -> export a sanitized Run Card -> submit it -> get featured
        ^                                                    |
        +-------------- fork the recipe/skill <--------------+
```

- [ ] Add a curated `showcase/` with prompt, redacted setup, result, receipt, and reproducibility notes.
- [ ] Feature one community workflow per release.
- [ ] Make each example forkable as a small recipe or skill, not a core feature.
- [ ] Add a validation job for submitted examples and skills.
- [ ] Never auto-upload receipts, prompts, repository names, or usage analytics.

### P4.3 Run evidence-led launches

1. Private alpha: 10–20 self-hosters validate install and safety.
2. `v0.1.0`: secure packaging, deterministic demo, and three examples.
3. `v0.2.0`: Run Receipts, approvals, durable jobs, and the flagship Telegram video.
4. Public launch: GitHub Release, concise technical article, Show HN, relevant Python/self-hosted communities, and Telegram demo posts.
5. Follow-up: publish real fixes, user workflows, benchmarks, and limitations instead of repeating the launch announcement.

The reusable launch pack should contain one headline, one 30-second clip, one five-minute quickstart, three recipes, one architecture/security diagram, raw benchmark data, and honest comparison tables.

### P4.4 Improve repository discovery

- [ ] Add a real homepage/docs URL to GitHub metadata.
- [ ] Keep topics precise: Telegram, Python, self-hosted, coding-agent, local-first, Codex, Claude.
- [ ] Use release notes and Discussions as recurring update channels.
- [ ] Reach a 100% community profile with useful files, not placeholders.
- [ ] Add badges only after the linked CI, release, package, and security signals are real.

## First 20 implementation issues

This order is designed to be copied into GitHub Issues. Items with dependencies should not start early merely because they look more exciting.

| ID | Priority | Issue | Depends on |
|---|---|---|---|
| LC-001 | P0 | Fail-closed Telegram owner pairing and authorization tests | — |
| LC-002 | P0 | Minimal delegated-process environment and secret-leak regression suite | — |
| LC-003 | P0 | Capability profiles and restrictive default execution policy | LC-002 |
| LC-004 | P0 | Worktree isolation, cancellation, and user-change-safe rollback | LC-003 |
| LC-005 | P0 | Collision-safe `pyproject.toml` and `lightclaw-ai` package smoke tests | — |
| LC-006 | P0 | App-specific config/data paths, `0600` secrets, migration, and uninstall dry-run | LC-005 |
| LC-007 | P0 | Pytest foundation with fake Telegram/provider/agent adapters | — |
| LC-008 | P0 | Ubuntu/macOS Python 3.10–3.13 CI and protected-main checks | LC-005, LC-007 |
| LC-009 | P0 | Security policy, threat model, Dependabot, CodeQL, and private reporting | LC-001–LC-004 |
| LC-010 | P1 | Deterministic `lightclaw demo` with a verified artifact | LC-005, LC-007 |
| LC-011 | P1 | `lightclaw doctor --json` with redacted diagnostics | LC-006 |
| LC-012 | P1 | Three reproducible example fixtures | LC-010 |
| LC-013 | P1 | Reproducible lightweight and reliability benchmark suite | LC-007, LC-012 |
| LC-014 | P1 | Result-first README, Telegram video, social card, and security diagram | LC-010–LC-013 |
| LC-015 | P1 | Trusted package publishing, changelog, and `v0.1.0` release | LC-008, LC-009, LC-014 |
| LC-016 | P2 | Local JSON/Markdown Run Receipt with redaction tests | LC-002, LC-004 |
| LC-017 | P2 | Telegram approval cards, risk previews, and result actions | LC-003, LC-016 |
| LC-018 | P2 | Durable queue/resume/cancel state machine | LC-004, LC-016 |
| LC-019 | P2 | Patch acceptance, selective apply, undo, and optional PR preview | LC-004, LC-016 |
| LC-020 | P3 | Memory truthfulness, FTS5 baseline, namespaces, retention, and evaluation | LC-007 |

## Public scorecard

Publish this scorecard in release notes or a generated status page. Do not add runtime telemetry to collect it.

| Funnel | Metric | Initial target |
|---|---|---:|
| Trust | Open critical/high security findings | 0 |
| Install | Clean macOS/Ubuntu install success | >= 90% in external alpha |
| Activation | Deterministic demo completion | >= 90% |
| Activation | Median deterministic time-to-success | < 3 minutes |
| Real use | Median first Telegram task | < 10 minutes |
| Reliability | Supported CI matrix | 100% green at release |
| Security | Unauthorized handler coverage | 100% |
| Security | Secret-leak regression cases | 0 leaks |
| Orchestration | Deterministic DAG/receipt scenario | 100 consecutive passes |
| Community | External contributors before broad launch | >= 5 |
| Community | Reproducible community showcase entries | >= 3 |
| Maintenance | Median first response to actionable issues | < 3 days |
| Release | Predictable release cadence | monthly or explicitly paused |

### Star milestones are campaign gates, not promises

| Awareness checkpoint | Proof required before pushing for it |
|---|---|
| First 100 stars | Secure defaults, green CI, deterministic demo, real `v0.1.0`, and 10 successful external installs |
| 500 stars | Signature Run Receipt workflow, flagship Telegram demo, three real showcase entries, and five external contributors |
| 1,000+ stars | Durable/recoverable execution, verified package/container distribution, a repeatable community workflow loop, and sustained maintenance |

No roadmap can guarantee stars. These gates ensure that attention converts into successful use instead of support debt or a security incident.

## Explicit non-goals through v1.0

- No race to add WhatsApp, Slack, Discord, or every chat channel.
- No large WebUI before the Telegram approval/result experience is excellent.
- No hosted cloud, accounts system, or telemetry backend.
- No unbounded autonomous self-improvement.
- No “swarm” marketing without deterministic task and safety evidence.
- No default access to the full host filesystem or environment.
- No provider addition without a maintainer and contract tests.
- No feature accepted only because a larger competitor has it.
- No benchmark claim without scripts, raw output, and named conditions.

## Definition of done for every roadmap item

An item is complete only when:

- behavior and non-goals are documented;
- threat/risk impact is considered;
- automated tests cover success and failure paths;
- a real runtime path is exercised where configuration checks are insufficient;
- user-visible errors explain recovery;
- secrets and personal data are absent from logs/fixtures;
- install/upgrade/uninstall impact is documented;
- CI is green;
- the README/changelog is updated when the public contract changes;
- measurable claims link to reproducible evidence.

## Research basis

This roadmap was checked against the current repository and primary project/documentation sources:

- [LightClaw repository API](https://api.github.com/repos/OthmaneBlial/lightclaw)
- [OpenClaw](https://github.com/openclaw/openclaw), [nanobot](https://github.com/HKUDS/nanobot), [NanoClaw](https://github.com/nanocoai/nanoclaw), [PicoClaw](https://github.com/sipeed/picoclaw), and [Hermes Agent](https://github.com/NousResearch/hermes-agent)
- [Browser Use](https://github.com/browser-use/browser-use), [CrewAI](https://github.com/crewAIInc/crewAI), [OpenHands](https://github.com/OpenHands/OpenHands), and [LangGraph](https://github.com/langchain-ai/langgraph)
- [GitHub community profiles](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/about-community-profiles-for-public-repositories)
- [GitHub security policies](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/add-security-policy)
- [GitHub protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
- [OpenSSF Scorecard](https://www.scorecard.dev/)
- [PyPA `pyproject.toml` specification](https://packaging.python.org/en/latest/specifications/pyproject-toml/)
- [PyPA Trusted Publishing guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)
- [Existing unrelated `lightclaw` distribution on PyPI](https://pypi.org/project/lightclaw/)

---

The winning version of LightClaw is not the one with the longest feature list. It is the one a developer can understand in 30 seconds, install safely in minutes, trust with a real repository, and proudly share after it delivers a verified result from Telegram.
