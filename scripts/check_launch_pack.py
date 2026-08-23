#!/usr/bin/env python3
"""Validate the reusable launch pack without inventing external launch evidence."""

from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from pathlib import Path

if __package__:
    from scripts.aggregate_alpha_reports import validate_aggregate
else:
    from aggregate_alpha_reports import validate_aggregate

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCH_ROOT = PROJECT_ROOT / "launch"
MANIFEST_PATH = LAUNCH_ROOT / "manifest.json"


def _load_object(path: Path, errors: list[str]) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{path.relative_to(PROJECT_ROOT)}: invalid JSON ({exc})")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path.relative_to(PROJECT_ROOT)}: expected a JSON object")
        return {}
    return value


def _resolve(value: object, label: str, errors: list[str]) -> Path | None:
    if not isinstance(value, str) or not value:
        errors.append(f"manifest: {label} must be a relative path")
        return None
    path = LAUNCH_ROOT / value
    try:
        path.resolve().relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        errors.append(f"manifest: {label} escapes the repository")
        return None
    if path.is_symlink() or not path.exists():
        errors.append(f"manifest: {label} is missing or a symlink ({value})")
        return None
    return path


def _validate_svg(path: Path, expected_duration: int | None, errors: list[str]) -> None:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        errors.append(f"{path.relative_to(PROJECT_ROOT)}: invalid SVG ({exc})")
        return
    if root.get("role") != "img" or "aria-labelledby" not in root.attrib:
        errors.append(f"{path.relative_to(PROJECT_ROOT)}: SVG needs an accessible image label")
    text = path.read_text(encoding="utf-8")
    if "<title" not in text or "<desc" not in text:
        errors.append(f"{path.relative_to(PROJECT_ROOT)}: SVG needs title and description")
    if expected_duration is not None and f'dur="{expected_duration}s"' not in text:
        errors.append(f"{path.relative_to(PROJECT_ROOT)}: clip duration does not match manifest")


def _validate_status(path: Path, errors: list[str]) -> None:
    status = _load_object(path, errors)
    if status.get("schema_version") != 1:
        errors.append("launch/status.json: schema_version must be 1")
    stages = status.get("stages")
    if not isinstance(stages, dict):
        errors.append("launch/status.json: stages must be an object")
        return
    alpha = stages.get("private_alpha")
    if not isinstance(alpha, dict) or alpha.get("completed_reports") != 0:
        errors.append("launch/status.json: alpha count must remain evidence-backed")
    for name in ("v0.1.0", "v0.2.0"):
        stage = stages.get(name)
        if not isinstance(stage, dict):
            errors.append(f"launch/status.json: missing {name} stage")
        elif stage.get("status") == "released" and not stage.get("release_url"):
            errors.append(f"launch/status.json: released {name} requires a release URL")
    public = status.get("public_launch")
    if isinstance(public, dict) and public.get("status") == "completed" and not public.get("evidence_url"):
        errors.append("launch/status.json: completed public launch requires evidence")


def validate_launch_pack() -> list[str]:
    errors: list[str] = []
    manifest = _load_object(MANIFEST_PATH, errors)
    if manifest.get("schema_version") != 1:
        errors.append("launch/manifest.json: schema_version must be 1")

    headline = _resolve(manifest.get("headline"), "headline", errors)
    if headline:
        lines = [line.strip() for line in headline.read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(lines) != 1 or len(lines[0]) > 120:
            errors.append("launch/HEADLINE.md: expected one factual line of at most 120 characters")

    clip = manifest.get("clip")
    if not isinstance(clip, dict) or not isinstance(clip.get("duration_seconds"), int):
        errors.append("launch manifest: clip requires an integer duration_seconds")
    else:
        clip_path = _resolve(clip.get("path"), "clip.path", errors)
        if clip_path:
            _validate_svg(clip_path, int(clip["duration_seconds"]), errors)
            if int(clip["duration_seconds"]) > 30:
                errors.append("launch manifest: clip must be at most 30 seconds")

    quickstart = _resolve(manifest.get("five_minute_quickstart"), "five_minute_quickstart", errors)
    if quickstart and "lightclaw demo --scenario repo-task" not in quickstart.read_text(encoding="utf-8"):
        errors.append("quickstart: deterministic repository demo command is missing")

    recipes = manifest.get("recipes")
    if not isinstance(recipes, list) or len(recipes) != 3:
        errors.append("launch manifest: exactly three recipes are required")
    else:
        for index, value in enumerate(recipes):
            recipe = _resolve(value, f"recipes[{index}]", errors)
            if recipe and not (recipe / "recipe.json").is_file():
                errors.append(f"{recipe.relative_to(PROJECT_ROOT)}: recipe.json is missing")

    diagram = _resolve(
        manifest.get("architecture_security_diagram"),
        "architecture_security_diagram",
        errors,
    )
    if diagram:
        _validate_svg(diagram, None, errors)

    raw = manifest.get("raw_benchmarks")
    if not isinstance(raw, list) or not raw:
        errors.append("launch manifest: raw benchmark paths are required")
    else:
        for index, value in enumerate(raw):
            path = _resolve(value, f"raw_benchmarks[{index}]", errors)
            if path is None:
                continue
            if path.suffix == ".json":
                _load_object(path, errors)
            elif path.suffix == ".csv":
                with path.open(encoding="utf-8", newline="") as handle:
                    if not list(csv.DictReader(handle)):
                        errors.append(f"{path.relative_to(PROJECT_ROOT)}: CSV has no data rows")
            else:
                errors.append(f"{path.relative_to(PROJECT_ROOT)}: benchmark must be JSON or CSV")

    comparisons = _resolve(manifest.get("comparisons"), "comparisons", errors)
    if comparisons:
        text = comparisons.read_text(encoding="utf-8")
        if "Claims LightClaw does not make" not in text or text.count("|---") < 1:
            errors.append("launch comparisons need a fit table and explicit non-claims")

    alpha = _resolve(manifest.get("private_alpha_aggregate"), "private_alpha_aggregate", errors)
    if alpha:
        aggregate = _load_object(alpha, errors)
        errors.extend(validate_aggregate(aggregate))

    status = _resolve(manifest.get("status"), "status", errors)
    if status:
        _validate_status(status, errors)
        if alpha:
            status_value = _load_object(status, errors)
            alpha_status = status_value.get("stages", {}).get("private_alpha", {})
            report_count = aggregate.get("collection", {}).get("report_count")
            if alpha_status.get("completed_reports") != report_count:
                errors.append("launch status: private alpha count must match the validated aggregate")
            if alpha_status.get("target_reports_min") != 10 or alpha_status.get("target_reports_max") != 20:
                errors.append("launch status: private alpha target must remain 10-20 reports")
            alpha_gate = aggregate.get("gates", {}).get("release_ready")
            expected_alpha_status = (
                "completed" if alpha_gate == "met" else "not_started" if report_count == 0 else "in_progress"
            )
            if alpha_status.get("status") != expected_alpha_status:
                errors.append("launch status: private alpha stage must match its evidence-derived gate")
    return errors


def main() -> int:
    errors = validate_launch_pack()
    if errors:
        print("Launch pack validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(
        "Launch pack validation passed: 1 headline, 24s clip, 5-minute quickstart, "
        "3 recipes, diagram, raw data, comparisons, and alpha evidence contract."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
