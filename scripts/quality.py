#!/usr/bin/env python3
"""Run the canonical contributor checks in a deterministic order."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def _run(label: str, command: list[str], *, retries: int = 0) -> None:
    print(f"\n==> {label}", flush=True)
    for attempt in range(retries + 1):
        completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        if not completed.returncode:
            return
        if attempt < retries:
            print(f"{label} failed under the current host load; retrying once.", flush=True)
    raise SystemExit(completed.returncode)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="lightclaw-quality-") as temporary:
        footprint = Path(temporary) / "runtime-footprint.json"
        wheel_directory = Path(temporary) / "dist"
        wheel_environment = Path(temporary) / "wheel-env"
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
        _run("documentation links", [PYTHON, "scripts/check_doc_links.py"])
        _run(
            "provider contract artifacts",
            [PYTHON, "scripts/generate_provider_matrix.py", "--check"],
        )
        _run("architecture budget", [PYTHON, "scripts/check_architecture.py", "--check"])
        for requirements in ("requirements-runtime.txt", "requirements-dev.txt"):
            _run(
                f"locked dependency resolution ({requirements})",
                [
                    PYTHON,
                    "-m",
                    "pip",
                    "install",
                    "--dry-run",
                    "--quiet",
                    "--require-hashes",
                    "-r",
                    requirements,
                ],
            )
        _run(
            "runtime footprint",
            [PYTHON, "-m", "bench.runtime_footprint", "--output", str(footprint)],
            retries=1,
        )
        _run(
            "safe skill fixture",
            [PYTHON, "-m", "lightclaw_cli", "skills", "validate", "--path", "examples/safe-skill"],
        )
        _run("showcase privacy and replay", [PYTHON, "scripts/validate_showcase.py", "--execute"])
        _run("private alpha evidence", [PYTHON, "scripts/aggregate_alpha_reports.py"])
        _run(
            "versioned release notes",
            [PYTHON, "scripts/check_release_notes.py", "docs/releases/v0.1.0.md", "--version", "0.1.0"],
        )
        _run("launch evidence pack", [PYTHON, "scripts/check_launch_pack.py"])
        _run("dependency audit", [PYTHON, "-m", "pip_audit", "--skip-editable"])
        _run("tests", [PYTHON, "-m", "pytest", "-q"])
        _run("package build", [PYTHON, "-m", "build", "--outdir", str(wheel_directory)])
        wheels = list(wheel_directory.glob("*.whl"))
        if len(wheels) != 1:
            raise SystemExit(f"Expected one built wheel, found {len(wheels)}")
        _run("clean wheel environment", [PYTHON, "-m", "venv", str(wheel_environment)])
        wheel_python = wheel_environment / "bin" / "python"
        _run(
            "install built wheel",
            [str(wheel_python), "-m", "pip", "install", "--no-deps", str(wheels[0])],
        )
        _run(
            "wheel data files",
            [str(wheel_python), str(PROJECT_ROOT / "scripts" / "check_installed_wheel_data.py")],
        )
    print("\nAll canonical LightClaw quality checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
