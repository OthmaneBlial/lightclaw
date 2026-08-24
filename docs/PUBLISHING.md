# Maintainer Release Runbook

Releases are built from GitHub-hosted clean environments. A signed stable tag starts the
release workflow, which verifies the tag against `pyproject.toml`, runs the full suite,
requires GitHub to verify its annotated signature, builds wheel/sdist, checks metadata,
creates an artifact attestation, publishes through
PyPI Trusted Publishing, pushes the GHCR image, and only then creates the GitHub Release
with the exact committed notes and verified assets. Manual dispatch does not publish by
default. A maintainer may explicitly enable `publish_prerelease` to
publish the current PEP 440 prerelease to PyPI after the same build, test, artifact, and
attestation steps. That path refuses stable versions and never publishes GHCR `latest` or
creates a stable GitHub Release.

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

## External alpha prerelease

Use the guarded manual prerelease path only to make an explicitly versioned development or
prerelease build installable by external alpha testers:

1. Keep a PEP 440 prerelease such as `0.1.0.dev0` in `pyproject.toml`.
2. Run the canonical quality command and push a green commit to `main`.
3. Dispatch `Release` with `publish_prerelease=true` and inspect the built distributions
   before approving the protected `pypi` environment.
4. Verify the exact version and hashes on PyPI, then install that exact version in a clean
   Python 3.10–3.13 environment and execute the deterministic repository-task demo.

This does not satisfy the stable-release gate, publish a stable GitHub Release, or turn
maintainer fixtures into external-alpha evidence. PyPI versions are immutable; fix a bad
upload with a new prerelease version rather than trying to replace its files.

If PyPI trusted publishing is not configured, the publish job must fail; do not bypass it with a token pasted into CI.
