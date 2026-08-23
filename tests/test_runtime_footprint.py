from __future__ import annotations

from bench import runtime_footprint


def test_runtime_footprint_dependency_and_budget_contract(monkeypatch):
    monkeypatch.setattr(runtime_footprint, "measure_cold_start", lambda _samples: [100.0])

    report = runtime_footprint.build_report(samples=1)
    dependencies = report["dependencies"]["direct"]

    assert runtime_footprint.validate_report(report) == []
    assert any(item.startswith("google-genai") for item in dependencies)
    assert not any(item.startswith("google-generativeai") for item in dependencies)
    assert "httpx==0.28.1" in dependencies
