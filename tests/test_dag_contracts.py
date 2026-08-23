from __future__ import annotations

from core.bot import LightClawBot


def test_explicit_dag_dependencies_are_preserved_without_cycles():
    planner = LightClawBot.__new__(LightClawBot)
    warnings: list[str] = []
    payload = planner._build_agents_plan_payload(
        "Build a fixture application",
        [("backend", "codex"), ("frontend", "claude"), ("review", "codex")],
        explicit_dependency_specs={
            "frontend": ["backend"],
            "review": ["backend", "frontend"],
        },
        warnings=warnings,
    )
    by_label = {item["label"]: item for item in payload["workers"]}

    assert by_label["backend"]["depends_on"] == []
    assert by_label["frontend"]["depends_on"] == ["backend"]
    assert by_label["review"]["depends_on"] == ["backend", "frontend"]


def test_explicit_dag_cycle_is_removed_deterministically():
    planner = LightClawBot.__new__(LightClawBot)
    workers = [
        {"label": "alpha", "depends_on": ["beta"]},
        {"label": "beta", "depends_on": ["alpha"]},
    ]
    warnings: list[str] = []

    planner._remove_multi_dependency_cycles(workers, warnings)

    assert workers == [
        {"label": "alpha", "depends_on": []},
        {"label": "beta", "depends_on": []},
    ]
    assert warnings == ["Explicit dependency cycle detected and removed for: alpha, beta"]
