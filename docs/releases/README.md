# Versioned Release Notes

Every stable release uses one committed `vX.Y.Z.md` file as the exact GitHub Release body.
The release workflow compares the published body with this file and rejects drafts,
placeholders, or missing install, verification, upgrade, uninstall, compatibility,
limitations, security, provenance, and rollback sections.

Prepare and validate a draft before tagging:

```bash
python scripts/check_release_notes.py docs/releases/v0.1.0.md --version 0.1.0
```

Immediately before release, remove the explicit draft warning and every placeholder, set
the stable package version in `pyproject.toml`, commit the final notes, then create and push
the signed tag:

```bash
git tag -s v0.1.0 -m "LightClaw v0.1.0"
git push origin v0.1.0
```

The tag workflow publishes PyPI and GHCR first, then creates the GitHub Release with this
exact notes file and its verified assets. Manual workflow dispatch performs rehearsal only.
Do not tag while the private-alpha aggregate or any security/release checklist gate is open.
