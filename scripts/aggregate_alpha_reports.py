#!/usr/bin/env python3
"""Validate private alpha reports and publish a privacy-safe aggregate."""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AGGREGATE = PROJECT_ROOT / "launch" / "alpha" / "aggregate.json"
REPORT_ID = re.compile(r"alpha-[0-9a-f]{12}")
VERSION = re.compile(r"(?:v)?[0-9]+\.[0-9]+\.[0-9]+(?:\.[a-z0-9]+)?|[0-9a-f]{7,40}")
PYTHON_VERSION = re.compile(r"3\.(?:10|11|12|13)(?:\.[0-9]+)?")
ALLOWED_REPORT_KEYS = {
    "schema_version",
    "report_id",
    "collected_at",
    "external_tester",
    "consent_aggregate",
    "lightclaw_version",
    "os_family",
    "python_version",
    "install_method",
    "install_attempted",
    "install_completed",
    "deterministic_demo_attempted",
    "deterministic_demo_completed",
    "deterministic_seconds",
    "telegram_attempted",
    "telegram_completed",
    "telegram_seconds",
    "artifact_receipt_produced",
}
ENUMS = {
    "os_family": {"linux", "macos", "other"},
    "install_method": {"pipx", "uv-tool", "venv", "container", "other"},
}


def _is_bool(value: object) -> bool:
    return isinstance(value, bool)


def validate_report(report: object, *, label: str = "report") -> list[str]:
    errors: list[str] = []
    if not isinstance(report, dict):
        return [f"{label}: expected a JSON object"]
    extra = sorted(set(report) - ALLOWED_REPORT_KEYS)
    missing = sorted(ALLOWED_REPORT_KEYS - set(report))
    if extra:
        errors.append(f"{label}: forbidden fields: {', '.join(extra)}")
    if missing:
        errors.append(f"{label}: missing fields: {', '.join(missing)}")
        return errors
    if report.get("schema_version") != 1:
        errors.append(f"{label}: schema_version must be 1")
    report_id = report.get("report_id")
    if not isinstance(report_id, str) or REPORT_ID.fullmatch(report_id) is None:
        errors.append(f"{label}: report_id must be alpha- plus 12 lowercase hex characters")
    collected_at = report.get("collected_at")
    try:
        date.fromisoformat(str(collected_at))
    except ValueError:
        errors.append(f"{label}: collected_at must be YYYY-MM-DD")
    version = report.get("lightclaw_version")
    if not isinstance(version, str) or VERSION.fullmatch(version) is None:
        errors.append(f"{label}: lightclaw_version must be a release or commit")
    python_version = report.get("python_version")
    if not isinstance(python_version, str) or PYTHON_VERSION.fullmatch(python_version) is None:
        errors.append(f"{label}: python_version must be within supported Python 3.10-3.13")
    for key, choices in ENUMS.items():
        if report.get(key) not in choices:
            errors.append(f"{label}: {key} must be one of {', '.join(sorted(choices))}")
    for key in (
        "external_tester",
        "consent_aggregate",
        "install_attempted",
        "install_completed",
        "deterministic_demo_attempted",
        "deterministic_demo_completed",
        "telegram_attempted",
        "telegram_completed",
    ):
        if not _is_bool(report.get(key)):
            errors.append(f"{label}: {key} must be boolean")
    if report.get("external_tester") is not True:
        errors.append(f"{label}: only external-tester evidence belongs in the alpha aggregate")
    if report.get("consent_aggregate") is not True:
        errors.append(f"{label}: explicit aggregate consent is required")
    if report.get("install_completed") and not report.get("install_attempted"):
        errors.append(f"{label}: install_completed requires install_attempted")
    if report.get("deterministic_demo_attempted") and not report.get("install_completed"):
        errors.append(f"{label}: deterministic demo requires a completed install")
    if report.get("deterministic_demo_completed") and not report.get("deterministic_demo_attempted"):
        errors.append(f"{label}: deterministic completion requires an attempted demo")
    if report.get("telegram_completed") and not report.get("telegram_attempted"):
        errors.append(f"{label}: Telegram completion requires an attempted Telegram task")
    for key, prerequisite in (
        ("deterministic_seconds", "deterministic_demo_completed"),
        ("telegram_seconds", "telegram_completed"),
    ):
        value = report.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            errors.append(f"{label}: {key} must be a positive integer or null")
        if value is not None and not report.get(prerequisite):
            errors.append(f"{label}: {key} requires {prerequisite}")
    artifact = report.get("artifact_receipt_produced")
    if artifact is not None and not _is_bool(artifact):
        errors.append(f"{label}: artifact_receipt_produced must be boolean or null")
    return errors


def _counts(reports: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(report[key]) for report in reports).items()))


def _median(values: list[int]) -> int | float | None:
    return statistics.median(values) if values else None


def build_aggregate(reports: list[dict[str, Any]]) -> dict[str, object]:
    dates = sorted(str(report["collected_at"]) for report in reports)
    deterministic_values = [
        int(report["deterministic_seconds"])
        for report in reports
        if report["deterministic_seconds"] is not None
    ]
    telegram_values = [
        int(report["telegram_seconds"])
        for report in reports
        if report["telegram_seconds"] is not None
    ]
    install_attempts = sum(bool(report["install_attempted"]) for report in reports)
    install_successes = sum(bool(report["install_completed"]) for report in reports)
    demo_attempts = sum(bool(report["deterministic_demo_attempted"]) for report in reports)
    demo_successes = sum(bool(report["deterministic_demo_completed"]) for report in reports)
    telegram_attempts = sum(bool(report["telegram_attempted"]) for report in reports)
    telegram_successes = sum(bool(report["telegram_completed"]) for report in reports)
    artifact_values = [report["artifact_receipt_produced"] for report in reports]
    cohort_met = 10 <= len(reports) <= 20 and install_attempts >= 10
    demo_success_met = install_attempts >= 10 and demo_successes >= 9
    deterministic_median = _median(deterministic_values)
    deterministic_median_met = (
        len(deterministic_values) >= 9
        and deterministic_median is not None
        and deterministic_median < 180
    )
    telegram_median = _median(telegram_values)
    telegram_median_met = (
        bool(telegram_values) and telegram_median is not None and telegram_median < 600
    )
    gates = {
        "cohort_10_to_20": "met" if cohort_met else "not_met",
        "nine_demo_successes": "met" if demo_success_met else "not_met",
        "deterministic_median_under_180_seconds": (
            "met" if deterministic_median_met else "not_met"
        ),
        "telegram_median_under_600_seconds": "met" if telegram_median_met else "not_met",
    }
    gates["release_ready"] = (
        "met" if all(value == "met" for value in gates.values()) else "not_met"
    )
    return {
        "schema_version": 1,
        "privacy": {
            "contains_identifiers": False,
            "contains_free_text": False,
            "raw_reports_committed": False,
        },
        "collection": {
            "report_count": len(reports),
            "unique_external_testers": len({report["report_id"] for report in reports}),
            "started_at": dates[0] if dates else None,
            "ended_at": dates[-1] if dates else None,
        },
        "tested": {
            "lightclaw_versions": _counts(reports, "lightclaw_version"),
            "os_families": _counts(reports, "os_family"),
            "python_versions": _counts(reports, "python_version"),
            "install_methods": _counts(reports, "install_method"),
        },
        "outcomes": {
            "install_attempts": install_attempts,
            "install_successes": install_successes,
            "install_failures": install_attempts - install_successes,
            "deterministic_demo_attempts": demo_attempts,
            "deterministic_demo_successes": demo_successes,
            "deterministic_demo_failures": demo_attempts - demo_successes,
            "deterministic_time_seconds": {
                "sample_count": len(deterministic_values),
                "missing_count": demo_successes - len(deterministic_values),
                "median": deterministic_median,
            },
            "telegram_attempts": telegram_attempts,
            "telegram_successes": telegram_successes,
            "telegram_failures": telegram_attempts - telegram_successes,
            "telegram_time_seconds": {
                "sample_count": len(telegram_values),
                "missing_count": telegram_successes - len(telegram_values),
                "median": telegram_median,
            },
            "artifact_receipt_successes": sum(value is True for value in artifact_values),
            "artifact_receipt_failures": sum(value is False for value in artifact_values),
            "artifact_receipt_missing": sum(value is None for value in artifact_values),
        },
        "gates": gates,
    }


def validate_aggregate(value: object) -> list[str]:
    if not isinstance(value, dict):
        return ["alpha aggregate: expected a JSON object"]
    errors: list[str] = []
    if value.get("schema_version") != 1:
        errors.append("alpha aggregate: schema_version must be 1")
    privacy = value.get("privacy")
    expected_privacy = {
        "contains_identifiers": False,
        "contains_free_text": False,
        "raw_reports_committed": False,
    }
    if privacy != expected_privacy:
        errors.append("alpha aggregate: privacy contract must exclude identifiers, free text, and raw reports")
    collection = value.get("collection")
    if not isinstance(collection, dict):
        errors.append("alpha aggregate: collection must be an object")
        return errors
    report_count = collection.get("report_count")
    unique = collection.get("unique_external_testers")
    if isinstance(report_count, bool) or not isinstance(report_count, int) or report_count < 0:
        errors.append("alpha aggregate: report_count must be a non-negative integer")
        return errors
    if unique != report_count:
        errors.append("alpha aggregate: every included report must represent one unique external tester")
    started_at = collection.get("started_at")
    ended_at = collection.get("ended_at")
    if report_count == 0 and (started_at is not None or ended_at is not None):
        errors.append("alpha aggregate: an empty collection cannot claim collection dates")
    elif report_count:
        try:
            started = date.fromisoformat(str(started_at))
            ended = date.fromisoformat(str(ended_at))
        except ValueError:
            errors.append("alpha aggregate: non-empty collection dates must use YYYY-MM-DD")
        else:
            if started > ended:
                errors.append("alpha aggregate: collection start must not be after its end")
    outcomes = value.get("outcomes")
    tested = value.get("tested")
    gates = value.get("gates")
    if not isinstance(outcomes, dict) or not isinstance(tested, dict) or not isinstance(gates, dict):
        errors.append("alpha aggregate: tested, outcomes, and gates must be objects")
        return errors
    for key in ("lightclaw_versions", "os_families", "python_versions", "install_methods"):
        counts = tested.get(key)
        valid_counts = (
            isinstance(counts, dict)
            and all(
                isinstance(name, str)
                and name
                and isinstance(number, int)
                and not isinstance(number, bool)
                and number > 0
                for name, number in counts.items()
            )
        )
        if not valid_counts or sum(counts.values()) != report_count:
            errors.append(f"alpha aggregate: tested.{key} counts must equal report_count")
    required_outcomes = (
        "install_attempts",
        "install_successes",
        "install_failures",
        "deterministic_demo_attempts",
        "deterministic_demo_successes",
        "deterministic_demo_failures",
        "telegram_attempts",
        "telegram_successes",
        "telegram_failures",
        "artifact_receipt_successes",
        "artifact_receipt_failures",
        "artifact_receipt_missing",
    )
    for key in required_outcomes:
        number = outcomes.get(key)
        if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= report_count:
            errors.append(f"alpha aggregate: outcomes.{key} must be between zero and report_count")
    if errors:
        return errors
    for prefix in ("install", "deterministic_demo", "telegram"):
        attempts = int(outcomes[f"{prefix}_attempts"])
        successes = int(outcomes[f"{prefix}_successes"])
        failures = int(outcomes[f"{prefix}_failures"])
        if successes + failures != attempts:
            errors.append(f"alpha aggregate: {prefix} successes plus failures must equal attempts")
    if outcomes["deterministic_demo_attempts"] > outcomes["install_successes"]:
        errors.append("alpha aggregate: deterministic attempts cannot exceed completed installs")
    if sum(int(outcomes[f"artifact_receipt_{suffix}"]) for suffix in ("successes", "failures", "missing")) != report_count:
        errors.append("alpha aggregate: artifact receipt counts must equal report_count")
    for key, successes_key in (
        ("deterministic_time_seconds", "deterministic_demo_successes"),
        ("telegram_time_seconds", "telegram_successes"),
    ):
        timing = outcomes.get(key)
        if not isinstance(timing, dict):
            errors.append(f"alpha aggregate: outcomes.{key} must be an object")
            continue
        samples = timing.get("sample_count")
        missing = timing.get("missing_count")
        median = timing.get("median")
        if not isinstance(samples, int) or not isinstance(missing, int) or samples < 0 or missing < 0:
            errors.append(f"alpha aggregate: outcomes.{key} sample/missing counts must be non-negative")
        elif samples + missing != outcomes[successes_key]:
            errors.append(f"alpha aggregate: outcomes.{key} coverage must equal successful outcomes")
        if samples == 0 and median is not None:
            errors.append(f"alpha aggregate: outcomes.{key} median must be null without samples")
        if samples and (isinstance(median, bool) or not isinstance(median, (int, float)) or median <= 0):
            errors.append(f"alpha aggregate: outcomes.{key} median must be positive with samples")
    if errors:
        return errors
    expected_gate_values = build_aggregate(
        _synthetic_reports_for_gate_recalculation(value)
    )["gates"]
    if gates != expected_gate_values:
        errors.append("alpha aggregate: gate statuses do not match the published counts and medians")
    return errors


def release_gate_errors(value: object) -> list[str]:
    """Return an explicit error unless a valid aggregate proves release readiness."""
    errors = validate_aggregate(value)
    if errors:
        return errors
    assert isinstance(value, dict)
    gates = value["gates"]
    assert isinstance(gates, dict)
    if gates.get("release_ready") != "met":
        return ["alpha aggregate: stable release gate is not met"]
    return []


def _synthetic_reports_for_gate_recalculation(aggregate: dict[str, Any]) -> list[dict[str, Any]]:
    """Reconstruct only the values needed to deterministically recalculate gates."""
    report_count = int(aggregate["collection"]["report_count"])
    outcomes = aggregate["outcomes"]
    deterministic = outcomes["deterministic_time_seconds"]
    telegram = outcomes["telegram_time_seconds"]
    reports: list[dict[str, Any]] = []
    for index in range(report_count):
        reports.append(
            {
                "report_id": f"alpha-{index:012x}",
                "collected_at": "2000-01-01",
                "lightclaw_version": "v0.0.0",
                "os_family": "other",
                "python_version": "3.10",
                "install_method": "other",
                "install_attempted": index < outcomes["install_attempts"],
                "install_completed": index < outcomes["install_successes"],
                "deterministic_demo_attempted": index < outcomes["deterministic_demo_attempts"],
                "deterministic_demo_completed": index < outcomes["deterministic_demo_successes"],
                "deterministic_seconds": (
                    deterministic["median"] if index < deterministic["sample_count"] else None
                ),
                "telegram_attempted": index < outcomes["telegram_attempts"],
                "telegram_completed": index < outcomes["telegram_successes"],
                "telegram_seconds": telegram["median"] if index < telegram["sample_count"] else None,
                "artifact_receipt_produced": None,
            }
        )
    return reports


def load_reports(directory: Path) -> tuple[list[dict[str, Any]], list[str]]:
    reports: list[dict[str, Any]] = []
    errors: list[str] = []
    if not directory.is_dir() or directory.is_symlink():
        return [], [f"alpha input must be a real directory: {directory}"]
    seen: set[str] = set()
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink():
            errors.append(f"{path}: symlinked reports are not accepted")
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: invalid JSON ({exc})")
            continue
        current = validate_report(report, label=path.name)
        errors.extend(current)
        if current or not isinstance(report, dict):
            continue
        report_id = str(report["report_id"])
        if report_id in seen:
            errors.append(f"{path.name}: duplicate report_id {report_id}")
            continue
        seen.add(report_id)
        reports.append(report)
    return reports, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Private directory of consented report JSON files")
    parser.add_argument("--output", type=Path, help="Write a privacy-safe aggregate")
    parser.add_argument("--check", type=Path, help="Validate/compare an aggregate")
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="Fail unless the validated aggregate proves every stable-release gate",
    )
    args = parser.parse_args()
    if args.input is None:
        check_path = args.check or DEFAULT_AGGREGATE
        try:
            aggregate = json.loads(check_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Alpha aggregate validation failed: {exc}")
            return 1
        errors = validate_aggregate(aggregate)
    else:
        reports, errors = load_reports(args.input.resolve())
        aggregate = build_aggregate(reports)
        errors.extend(validate_aggregate(aggregate))
        if not args.output and not args.check:
            errors.append("alpha aggregation requires --output or --check")
        if args.check:
            try:
                expected = json.loads(args.check.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"cannot read aggregate to check: {exc}")
            else:
                if aggregate != expected:
                    errors.append("alpha aggregate is stale for the supplied private reports")
    if args.require_ready and not errors:
        errors.extend(release_gate_errors(aggregate))
    if args.output and args.input is not None and not errors:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(aggregate, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if errors:
        print("Alpha aggregate validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(
        "Alpha aggregate validation passed: "
        f"{aggregate['collection']['report_count']} consented external reports; "
        f"release gate {aggregate['gates']['release_ready']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
