#!/usr/bin/env python3
"""Validate versioned release notes and, on release, their published GitHub body."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SECTIONS = (
    "Install",
    "Verify",
    "Upgrade",
    "Uninstall",
    "Compatibility",
    "Security boundaries",
    "Known limitations",
    "Artifacts and provenance",
    "Rollback",
)
FORBIDDEN_PUBLISHED_MARKERS = ("DRAFT", "not released", "TODO", "[release URL]", "TBD")


def validate_release_notes(
    text: str,
    *,
    version: str,
    published_body: str | None = None,
) -> list[str]:
    errors: list[str] = []
    if not text.startswith(f"# LightClaw v{version}\n"):
        errors.append(f"release notes: first heading must be '# LightClaw v{version}'")
    for section in REQUIRED_SECTIONS:
        if f"\n## {section}\n" not in text:
            errors.append(f"release notes: missing section '{section}'")
    required_evidence = {
        "install command": f"lightclaw-ai=={version}",
        "deterministic verification": "lightclaw demo --scenario repo-task",
        "doctor verification": "lightclaw doctor --json",
        "uninstall command": "pipx uninstall lightclaw-ai",
        "supported Python range": "Python 3.10–3.13",
        "rollback tag": f"v{version}",
        "private data boundary": "local and private by default",
    }
    for label, needle in required_evidence.items():
        if needle not in text:
            errors.append(f"release notes: missing {label}")
    if published_body is not None:
        for marker in FORBIDDEN_PUBLISHED_MARKERS:
            if marker.casefold() in text.casefold():
                errors.append(f"release notes: published notes contain forbidden marker '{marker}'")
        if _normalize(text) != _normalize(published_body):
            errors.append("release notes: GitHub release body differs from the committed versioned notes")
    return errors


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def _version_from_tag(tag: str) -> str | None:
    match = re.fullmatch(r"v([0-9]+\.[0-9]+\.[0-9]+)", tag)
    return match.group(1) if match else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--event", type=Path, help="GitHub release event JSON")
    parser.add_argument("--published", action="store_true", help="Reject draft markers")
    args = parser.parse_args()
    published_body: str | None = None
    if args.event:
        try:
            event = json.loads(args.event.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Release notes validation failed: invalid release event ({exc})")
            return 1
        release = event.get("release")
        if not isinstance(release, dict):
            print("Release notes validation failed: event has no release object")
            return 1
        tag = release.get("tag_name")
        version = _version_from_tag(tag) if isinstance(tag, str) else None
        published_body = release.get("body")
        if version is None or not isinstance(published_body, str):
            print("Release notes validation failed: release tag/body is invalid")
            return 1
        path = PROJECT_ROOT / "docs" / "releases" / f"v{version}.md"
    else:
        path = args.path
        version = args.version
    if path is None or version is None:
        parser.error("provide path plus --version, or --event")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Release notes validation failed: {exc}")
        return 1
    if args.published and published_body is None:
        published_body = text
    errors = validate_release_notes(text, version=version, published_body=published_body)
    if errors:
        print("Release notes validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    mode = "published" if published_body is not None else "draft"
    print(f"Release notes validation passed for v{version} ({mode} contract).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
