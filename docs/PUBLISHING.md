# Maintainer Release Runbook

Releases are built from GitHub-hosted clean environments. A signed stable tag starts the
release workflow, which verifies the tag against `pyproject.toml`, runs the full suite,
requires GitHub to verify its annotated signature, builds wheel/sdist, checks metadata,
creates an artifact attestation, publishes through
PyPI Trusted Publishing, pushes the GHCR image, and only then creates the GitHub Release
with the exact committed notes and verified assets. Manual dispatch is rehearsal-only and
cannot publish packages or containers.

## One-time PyPI setup

1. Create or reserve the `lightclaw-ai` project on PyPI.
2. Add a trusted publisher for owner `OthmaneBlial`, repository `lightclaw`, workflow `release.yml`, environment `pypi`.
3. Create a protected GitHub environment named `pypi` and require reviewer approval.
4. Do not create a long-lived PyPI API token or repository secret.

## Release checklist

1. Ensure every Phase 0 gate and deterministic demo is green.
2. Set the exact version in `pyproject.toml`; update date and links in `CHANGELOG.md`, then
   finalize `docs/releases/vX.Y.Z.md`. The workflow requires the published GitHub body to
   match that file exactly and refuses draft markers.
3. Build locally and inspect `twine check dist/*`.
4. Push the release commit and wait for all required checks.
5. Create and push the signed Git tag with the matching `vX.Y.Z` name. Do not create the
   GitHub Release manually; the workflow creates it only after PyPI and GHCR succeed.
6. Approve the protected `pypi` environment only after inspecting the workflow's built artifacts.
7. Verify PyPI metadata/attestations, GitHub assets/attestation, GHCR digest, clean pipx/uv install, deterministic demo, and rollback instructions.

If PyPI trusted publishing is not configured, the publish job must fail; do not bypass it with a token pasted into CI.
