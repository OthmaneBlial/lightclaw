from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_committed_architecture_budget_report_is_current():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/check_architecture.py", "--check"],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
