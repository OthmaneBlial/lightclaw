#!/usr/bin/env python3
"""Run the canonical contributor checks in a deterministic order."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def _run(label: str, command: list[str]) -> None:
    print(f"\n==> {label}", flush=True)
    completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    if completed.returncode:
        raise SystemExit(completed.returncode)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="lightclaw-quality-") as temporary:
        footprint = Path(temporary) / "runtime-footprint.json"
        _run(
            "lint",
            [
                PYTHON,
                "-m",
                "ruff",
                "check",
                "config.py",
                "lightclaw_cli.py",
                "main.py",
                "memory.py",
                "providers.py",
                "skills.py",
                "core",
                "tests",
                "bench",
                "scripts",
            ],
        )
        _run(
            "provider contract artifacts",
            [PYTHON, "scripts/generate_provider_matrix.py", "--check"],
        )
        _run("architecture budget", [PYTHON, "scripts/check_architecture.py", "--check"])
        _run(
            "runtime footprint",
            [PYTHON, "-m", "bench.runtime_footprint", "--output", str(footprint)],
        )
        _run(
            "safe skill fixture",
            [PYTHON, "-m", "lightclaw_cli", "skills", "validate", "--path", "examples/safe-skill"],
        )
        _run("showcase privacy and replay", [PYTHON, "scripts/validate_showcase.py", "--execute"])
        _run("launch evidence pack", [PYTHON, "scripts/check_launch_pack.py"])
        _run("tests", [PYTHON, "-m", "pytest", "-q"])
        _run("package build", [PYTHON, "-m", "build"])
    print("\nAll canonical LightClaw quality checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
