from copy import deepcopy
from pathlib import Path

from scripts.aggregate_alpha_reports import (
    build_aggregate,
    release_gate_errors,
    validate_aggregate,
    validate_report,
)

ROOT = Path(__file__).resolve().parents[1]


def _report(index: int, *, demo_completed: bool = True) -> dict[str, object]:
    return {
        "schema_version": 1,
        "report_id": f"alpha-{index:012x}",
        "collected_at": "2026-08-24",
        "external_tester": True,
        "consent_aggregate": True,
        "lightclaw_version": "0123abc",
        "os_family": "linux" if index % 2 else "macos",
        "python_version": "3.13",
        "install_method": "pipx",
        "install_attempted": True,
        "install_completed": True,
        "deterministic_demo_attempted": True,
        "deterministic_demo_completed": demo_completed,
        "deterministic_seconds": 120 + index if demo_completed else None,
        "telegram_attempted": index < 5,
        "telegram_completed": index < 5,
        "telegram_seconds": 420 + index if index < 5 else None,
        "artifact_receipt_produced": demo_completed,
    }


def test_committed_empty_alpha_aggregate_is_honest_and_valid():
    aggregate = build_aggregate([])
    assert aggregate["collection"]["report_count"] == 0
    assert aggregate["gates"]["release_ready"] == "not_met"
    assert validate_aggregate(aggregate) == []


def test_public_alpha_form_defaults_cannot_claim_success():
    form = (ROOT / ".github" / "ISSUE_TEMPLATE" / "alpha.yml").read_text(encoding="utf-8")
    assert 'options: ["No", "Yes"]' in form
    assert 'options: ["Not attempted because install failed", "No", "Yes"]' in form
    assert form.count('options: ["Not attempted", "No", "Yes"]') == 2
    assert "options: [Other/unspecified, Linux, macOS]" in form
    assert "options: [Other/unspecified, pipx" in form
    assert "id: python\n    attributes:\n      label: Python version\n      description:" in form


def test_synthetic_external_cohort_meets_numeric_gates():
    reports = [_report(index, demo_completed=index < 9) for index in range(10)]
    assert all(validate_report(report) == [] for report in reports)
    aggregate = build_aggregate(reports)
    assert aggregate["outcomes"]["deterministic_demo_successes"] == 9
    assert aggregate["outcomes"]["deterministic_time_seconds"]["median"] == 124
    assert aggregate["gates"]["release_ready"] == "met"
    assert validate_aggregate(aggregate) == []
    assert release_gate_errors(aggregate) == []


def test_empty_alpha_aggregate_blocks_a_stable_release():
    assert release_gate_errors(build_aggregate([])) == [
        "alpha aggregate: stable release gate is not met"
    ]


def test_report_rejects_identity_or_free_text_fields():
    report = _report(1)
    report["telegram_username"] = "forbidden"
    assert validate_report(report) == ["report: forbidden fields: telegram_username"]


def test_aggregate_rejects_manually_inflated_gate_state():
    aggregate = deepcopy(build_aggregate([]))
    aggregate["gates"]["release_ready"] = "met"
    assert validate_aggregate(aggregate) == [
        "alpha aggregate: gate statuses do not match the published counts and medians"
    ]


def test_aggregate_rejects_malformed_environment_counts_without_crashing():
    aggregate = deepcopy(build_aggregate([]))
    aggregate["tested"]["python_versions"] = {"3.13": "zero"}
    assert validate_aggregate(aggregate) == [
        "alpha aggregate: tested.python_versions counts must equal report_count"
    ]
