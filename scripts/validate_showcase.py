#!/usr/bin/env python3
"""Validate privacy, provenance, and reproducibility of public showcase entries."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = PROJECT_ROOT / "showcase"
REQUIRED_ROLES = {"prompt", "setup", "result", "receipt", "reproduce", "recipe"}
PRIVATE_RECEIPT_FIELDS = {"commands", "handoffs", "checkpoint", "undo", "started_at", "finished_at"}
SENSITIVE_PATTERNS = {
    "macOS user path": re.compile(r"/Users/[^/\s]+/"),
    "Linux user path": re.compile(r"/home/[^/\s]+/"),
    "Windows user path": re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+\\"),
    "Telegram bot token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),
    "common API key": re.compile(r"\b(?:sk|xai)-[A-Za-z0-9_-]{16,}\b"),
    "Google API key": re.compile(r"\bAIza[A-Za-z0-9_-]{24,}\b"),
}


def _json_object(path: Path, errors: list[str]) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"{path.relative_to(PROJECT_ROOT)}: invalid JSON ({exc})")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path.relative_to(PROJECT_ROOT)}: expected a JSON object")
        return {}
    return value


def _relative_file(entry: Path, value: object, role: str, errors: list[str]) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{entry.name}: files.{role} must be a non-empty relative path")
        return None
    candidate = entry / value
    try:
        candidate.resolve().relative_to(entry.resolve())
    except ValueError:
        errors.append(f"{entry.name}: files.{role} escapes the entry directory")
        return None
    if candidate.is_symlink() or not candidate.is_file():
        errors.append(f"{entry.name}: files.{role} is missing or a symlink ({value})")
        return None
    return candidate


def _scan_public_file(path: Path, errors: list[str]) -> None:
    if path.stat().st_size > 131_072:
        errors.append(f"{path.relative_to(PROJECT_ROOT)}: public evidence exceeds 128 KiB")
        return
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        errors.append(f"{path.relative_to(PROJECT_ROOT)}: public evidence must be UTF-8 text")
        return
    for label, pattern in SENSITIVE_PATTERNS.items():
        if pattern.search(text):
            errors.append(f"{path.relative_to(PROJECT_ROOT)}: contains a {label}")


def _validate_run_card(path: Path, errors: list[str]) -> None:
    card = _json_object(path, errors)
    if card.get("schema_version") != 1 or card.get("share_card") is not True:
        errors.append(f"{path.relative_to(PROJECT_ROOT)}: expected schema_version=1 share card")
    present = sorted(PRIVATE_RECEIPT_FIELDS & set(card))
    if present:
        errors.append(
            f"{path.relative_to(PROJECT_ROOT)}: contains private receipt fields: {', '.join(present)}"
        )
    excluded = card.get("excluded_private_fields")
    if not isinstance(excluded, list) or not PRIVATE_RECEIPT_FIELDS.issubset(set(excluded)):
        errors.append(f"{path.relative_to(PROJECT_ROOT)}: private-field exclusion is incomplete")
    if card.get("disposition") not in {"accepted", "ready_for_review"}:
        errors.append(f"{path.relative_to(PROJECT_ROOT)}: only successful reviewable runs qualify")


def _execute_recipe(entry: Path, recipe: dict[str, object], errors: list[str]) -> None:
    command = recipe.get("command")
    expected = recipe.get("expected_artifacts")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        errors.append(f"{entry.name}: recipe.command must be a non-empty string array")
        return
    if not isinstance(expected, list) or not expected or not all(isinstance(item, str) for item in expected):
        errors.append(f"{entry.name}: recipe.expected_artifacts must be a non-empty string array")
        return
    if command[0] != "lightclaw" or "{output}" not in command:
        errors.append(f"{entry.name}: recipe command must use lightclaw and an explicit {{output}}")
        return
    with tempfile.TemporaryDirectory(prefix=f"lightclaw-showcase-{entry.name}-") as temporary:
        output = Path(temporary) / "output"
        resolved = [output.as_posix() if token == "{output}" else token for token in command[1:]]
        completed = subprocess.run(
            [sys.executable, "-m", "lightclaw_cli", *resolved],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip().splitlines()[-1:]
            errors.append(f"{entry.name}: recipe failed ({'; '.join(detail) or 'no output'})")
            return
        for relative in expected:
            artifact = output / relative
            try:
                artifact.resolve().relative_to(output.resolve())
            except ValueError:
                errors.append(f"{entry.name}: expected artifact escapes output ({relative})")
                continue
            if not artifact.is_file() or artifact.stat().st_size == 0:
                errors.append(f"{entry.name}: expected artifact missing or empty ({relative})")


def _validate_recipe(entry: Path, path: Path, execute: bool, errors: list[str]) -> None:
    recipe = _json_object(path, errors)
    if recipe.get("schema_version") != 1 or recipe.get("kind") != "recipe":
        errors.append(f"{entry.name}: recipe must declare schema_version=1 and kind=recipe")
    if recipe.get("network_required") is not False or recipe.get("credentials_required") is not False:
        errors.append(f"{entry.name}: curated recipes must be token-free and network-free")
    if execute:
        _execute_recipe(entry, recipe, errors)


def _validate_skill(entry: Path, skill_path: Path, errors: list[str]) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "lightclaw_cli", "skills", "validate", "--path", str(skill_path.parent)],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode:
        errors.append(f"{entry.name}: skill permission contract failed validation")


def _validate_entry(entry: Path, execute: bool, errors: list[str]) -> dict[str, object]:
    manifest_path = entry / "showcase.json"
    manifest = _json_object(manifest_path, errors)
    if manifest.get("schema_version") != 1:
        errors.append(f"{entry.name}: showcase schema_version must be 1")
    if manifest.get("slug") != entry.name:
        errors.append(f"{entry.name}: slug must match its directory")
    if manifest.get("kind") not in {"recipe", "skill"}:
        errors.append(f"{entry.name}: kind must be recipe or skill")

    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("type") not in {
        "maintainer-fixture",
        "community",
    }:
        errors.append(f"{entry.name}: provenance must identify a maintainer fixture or community source")
    elif provenance.get("type") == "community":
        if provenance.get("publication_consent") is not True or not provenance.get("source_url"):
            errors.append(f"{entry.name}: community provenance requires consent and a source URL")

    privacy = manifest.get("privacy")
    required_privacy = {
        "synthetic_or_publishable_input",
        "run_card_only",
        "repository_names_removed",
        "local_paths_removed",
        "credentials_removed",
    }
    if not isinstance(privacy, dict) or any(privacy.get(key) is not True for key in required_privacy):
        errors.append(f"{entry.name}: all privacy attestations must be true")

    files = manifest.get("files")
    if not isinstance(files, dict):
        errors.append(f"{entry.name}: files must map every evidence role")
        return manifest
    missing_roles = sorted(REQUIRED_ROLES - set(files))
    if missing_roles:
        errors.append(f"{entry.name}: missing evidence roles: {', '.join(missing_roles)}")

    resolved: dict[str, Path] = {}
    for role in REQUIRED_ROLES:
        path = _relative_file(entry, files.get(role), role, errors)
        if path is not None:
            resolved[role] = path
            _scan_public_file(path, errors)
    if "receipt" in resolved:
        _validate_run_card(resolved["receipt"], errors)
    if manifest.get("kind") == "recipe" and "recipe" in resolved:
        _validate_recipe(entry, resolved["recipe"], execute, errors)
    if manifest.get("kind") == "skill" and "recipe" in resolved:
        _validate_skill(entry, resolved["recipe"], errors)
    return manifest


def validate_showcase(
    root: Path = DEFAULT_ROOT,
    *,
    execute: bool = False,
    require_community_feature: bool = False,
) -> list[str]:
    errors: list[str] = []
    entries_root = root / "entries"
    entries = sorted(path for path in entries_root.iterdir() if path.is_dir()) if entries_root.is_dir() else []
    if not entries:
        return [f"{entries_root}: no showcase entries found"]
    manifests = {entry.name: _validate_entry(entry, execute, errors) for entry in entries}

    featured = _json_object(root / "featured.json", errors)
    current = featured.get("current")
    if current is not None:
        if current not in manifests:
            errors.append("featured.json: current entry does not exist")
        else:
            provenance = manifests[current].get("provenance")
            if not isinstance(provenance, dict) or provenance.get("type") != "community":
                errors.append("featured.json: featured entry must have community provenance")
    if require_community_feature and current is None:
        errors.append("featured.json: a consented community entry is required for this release")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--execute", action="store_true", help="Replay token-free recipes")
    parser.add_argument(
        "--require-community-feature",
        action="store_true",
        help="Fail unless featured.json selects a consented community entry",
    )
    args = parser.parse_args()
    errors = validate_showcase(
        args.root.resolve(),
        execute=args.execute,
        require_community_feature=args.require_community_feature,
    )
    if errors:
        print("Showcase validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    count = len([path for path in (args.root / "entries").iterdir() if path.is_dir()])
    mode = "static and replay" if args.execute else "static"
    print(f"Showcase validation passed for {count} entries ({mode}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

