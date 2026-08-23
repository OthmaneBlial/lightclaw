from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_runtime_footprint_dependency_and_budget_contract(tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "runtime-footprint.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "bench.runtime_footprint",
            "--samples",
            "1",
            "--output",
            output.as_posix(),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    dependencies = report["dependencies"]["direct"]
    assert report["budget_passed"] is True
    assert any(item.startswith("google-genai") for item in dependencies)
    assert not any(item.startswith("google-generativeai") for item in dependencies)
