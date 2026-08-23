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
| PyPI Trusted Publishing | OIDC/attestation workflow and distinct distribution are configured; nothing is published | Live `lightclaw-ai` PyPI version, trusted-publisher provenance, and successful clean install |
| `v0.1.0` release | Release workflow, notes configuration, checklist, changelog, upgrade, and rollback docs exist; no tag/release exists | Published GitHub Release after its source commit has all required green checks |
| Complete release notes | Required subjects are documented but no release notes exist | Versioned notes covering install, upgrade, uninstall, compatibility, limitations, security, and rollback |
| GHCR container | Release workflow can publish a versioned image and the systemd guide exists; no image digest exists | Public image/tag/digest built from the release commit plus verified command/container smoke |
| 10 external installs / 9 demo successes | Alpha template exists; completed external reports: 0 | Sanitized aggregate with 10–20 attempts, at least 9 successes, versions, date window, and failures |
| Median deterministic success under 3 minutes | No external timing sample exists | Median from the same external alpha cohort with denominator and missing/failed attempts |
| Median real Telegram task under 10 minutes | No external real-device timing sample exists | Sanitized real Telegram timing aggregate with denominator and failure handling |
| Verified `v0.1.0` artifacts | No release exists | Wheel/sdist, attestations, runtime footprint, notes, and rollback links on the live release |
| Five external bounded repo tasks | Maintainer fixtures are green; external completions: 0 | Five consented reports showing a real bounded task and user explanation of the change |
| Three voluntary public Run Cards | Three maintainer fixture cards exist; voluntary external cards: 0 | Three privacy-reviewed community entries with provenance and publication consent |
| One community workflow per release | `showcase/featured.json` is intentionally `null` | A consented external entry selected for each applicable release |

## Implemented machinery behind those gates

- Trusted PyPI OIDC, build provenance attestations, GitHub Release assets, and GHCR
  publication are defined in the release workflow.
- `lightclaw demo` and all three showcase recipes are token-free and replayed by CI.
- The [alpha report template](../launch/ALPHA_REPORT_TEMPLATE.md) collects bounded outcomes
  without raw prompts, receipts, repository identities, paths, or credentials.
- The [release checklist](../launch/RELEASE_CHECKLIST.md) refuses promotion when security,
  compatibility, rollback, alpha, or community evidence is absent.
- `showcase/featured.json` and `--require-community-feature` make the community gap
  machine-visible instead of silently substituting maintainer fixtures.
- [launch/status.json](../launch/status.json) records external stages as not started/not
  released and requires evidence URLs before a completed state is valid.

## Current reproducible repository evidence

- Canonical local quality command: lint, provider matrix, architecture/runtime budgets,
  skill contract, showcase privacy/replay, launch-pack validation, full tests, and package
  build.
- Test suite: 113 passing tests in the final local audit; the sole local warning comes from
  `google-genai` on an unsupported-for-release Python 3.14 interpreter. Supported CI uses
  Python 3.10–3.13.
- Latest completed implementation commit before this audit: CI, CodeQL, and OpenSSF
  Scorecard all succeeded, including Ubuntu and macOS Python 3.10–3.13.
- GitHub controls: private vulnerability reporting and Discussions enabled, 100% community
  profile, active CI/CodeQL workflows, precise topics, and a live docs URL.

Live state can change after this snapshot. Release and external-adoption gates must be
rechecked at the time they are claimed; this document is not a substitute for their URLs.

