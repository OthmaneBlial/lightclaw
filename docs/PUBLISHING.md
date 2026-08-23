# Maintainer Release Runbook

Releases are built from GitHub-hosted clean environments. The release workflow verifies the tag against `pyproject.toml`, runs the full suite, builds wheel/sdist, checks their metadata, creates an artifact attestation, attaches distributions to the GitHub release, publishes through PyPI Trusted Publishing, and pushes an optional GHCR image.

## One-time PyPI setup

1. Create or reserve the `lightclaw-ai` project on PyPI.
2. Add a trusted publisher for owner `OthmaneBlial`, repository `lightclaw`, workflow `release.yml`, environment `pypi`.
3. Create a protected GitHub environment named `pypi` and require reviewer approval.
4. Do not create a long-lived PyPI API token or repository secret.

## Release checklist

1. Ensure every Phase 0 gate and deterministic demo is green.
2. Set the exact version in `pyproject.toml`; update date and links in `CHANGELOG.md`.
3. Build locally and inspect `twine check dist/*`.
4. Push the release commit and wait for all required checks.
5. Create the signed Git tag and GitHub release with the matching `vX.Y.Z` name.
6. Approve the protected `pypi` environment only after inspecting the workflow's built artifacts.
7. Verify PyPI metadata/attestations, GitHub assets/attestation, GHCR digest, clean pipx/uv install, deterministic demo, and rollback instructions.

If PyPI trusted publishing is not configured, the publish job must fail; do not bypass it with a token pasted into CI.
