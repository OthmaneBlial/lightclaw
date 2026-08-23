from __future__ import annotations

import json
import stat

import pytest

from core.demo import DEMO_SCENARIOS, run_demo


@pytest.mark.parametrize("scenario", DEMO_SCENARIOS)
def test_deterministic_demo_scenarios_finish_with_artifact_and_receipt(tmp_path, scenario):
    output = tmp_path / scenario
    result = run_demo(scenario, output)

    assert result["ok"] is True
    assert result["artifacts"]
    assert all(check["passed"] for check in result["checks"])
    receipt = json.loads((output / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["fixture"] is True
    assert receipt["usage"] == {"estimated_cost_usd": 0, "provider": "fixture", "tokens": 0}
    assert receipt["disposition"] == "accepted"
    assert stat.S_IMODE((output / "receipt.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((output / "receipt.md").stat().st_mode) == 0o600


def test_demo_refuses_nonempty_output_directory(tmp_path):
    output = tmp_path / "occupied"
    output.mkdir()
    (output / "user-file.txt").write_text("preserve me", encoding="utf-8")

    with pytest.raises(ValueError, match="must be empty"):
        run_demo("repo-task", output)

    assert (output / "user-file.txt").read_text(encoding="utf-8") == "preserve me"
