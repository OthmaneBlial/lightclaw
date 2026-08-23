# Roadmap Evidence Audit

**Audit date:** 2026-08-24  
**Result:** repository-controlled implementation is complete; 11 live release/adoption
evidence gates remain open.

The unchecked items in [ROADMAP.md](../ROADMAP.md) are not missing code tasks. They require
publication to third-party registries or voluntary evidence from people outside the
maintainer-authored fixtures. Checking them without that evidence would violate the
roadmap's “proof over claims” rule.

## Open gates

| Roadmap gate | Current truthful state | Evidence required to close it |
|---|---|---|
| PyPI Trusted Publishing | OIDC/attestation workflow, protected `pypi` environment, and distinct distribution are configured; PyPI account-side publisher setup still requires an authenticated maintainer and nothing is published | Live `lightclaw-ai` PyPI version, trusted-publisher provenance, and successful clean install |
| `v0.1.0` release | Release workflow, notes configuration, checklist, changelog, upgrade, and rollback docs exist; no tag/release exists | Published GitHub Release after its source commit has all required green checks |
| Complete release notes | A versioned `v0.1.0` draft covers every required subject and CI enforces an identical placeholder-free release body; no notes are published | Finalized versioned notes on the live release covering install, upgrade, uninstall, compatibility, limitations, security, provenance, and rollback |
| GHCR container | Release workflow can publish a versioned image and the systemd guide exists; no image digest exists | Public image/tag/digest built from the release commit plus verified command/container smoke |
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
- `lightclaw demo` and all three showcase recipes are token-free and replayed by CI.
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
- Test suite: 121 passing tests in the current local audit; the sole local warning comes from
  `google-genai` on an unsupported-for-release Python 3.14 interpreter. Supported CI uses
  Python 3.10–3.13.
- Latest implementation commit `213decb`: CI, CodeQL, and OpenSSF Scorecard all succeeded,
  including Ubuntu and macOS Python 3.10–3.13.
- The release workflow's manual rehearsal succeeded at that commit: verified distributions,
  runtime footprint, attestations, and uploaded workflow artifacts were produced while
  PyPI, GHCR, and GitHub Release publication jobs remained skipped by contract.
- A local Linux container build from the preceding alpha-evidence commit succeeded under
  an unprivileged user; `--read-only` plus temporary filesystems completed the deterministic
  memory demo. This is pre-release smoke evidence, not a substitute for a public GHCR digest.
- GitHub controls: private vulnerability reporting and Discussions enabled, 100% community
  profile, active CI/CodeQL workflows, precise topics, and a live docs URL.

Live state can change after this snapshot. Release and external-adoption gates must be
rechecked at the time they are claimed; this document is not a substitute for their URLs.
