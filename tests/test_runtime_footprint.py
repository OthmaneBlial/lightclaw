from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by the Python 3.10 CI lane
    import tomli as tomllib

from bench import runtime_footprint


def test_runtime_footprint_dependency_and_budget_contract(monkeypatch):
    monkeypatch.setattr(runtime_footprint, "measure_cold_start", lambda _samples: [100.0])

    report = runtime_footprint.build_report(samples=1)
    dependencies = report["dependencies"]["direct"]
    project = tomllib.loads(
        (runtime_footprint.PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    optional = project["optional-dependencies"]

    assert runtime_footprint.validate_report(report) == []
    assert len(dependencies) == 3
    assert "httpx==0.28.1" in dependencies
    assert any(item.startswith("openai") for item in optional["openai"])
    assert any(item.startswith("anthropic") for item in optional["claude"])
    assert any(item.startswith("google-genai") for item in optional["gemini"])
    assert not any(item.startswith("google-generativeai") for item in optional["providers"])
