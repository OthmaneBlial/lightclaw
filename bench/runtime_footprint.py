"""Measure cold import and dependency/wheel growth against release budgets."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUDGET_PATH = PROJECT_ROOT / "docs" / "architecture" / "core-budget.json"


def read_direct_dependencies() -> list[str]:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - exercised by the Python 3.10 CI lane
        import tomli as tomllib
    payload = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return sorted(str(item) for item in payload.get("project", {}).get("dependencies", []))


def measure_cold_start(samples: int) -> list[float]:
    results: list[float] = []
    command = [sys.executable, "-c", "import lightclaw_cli"]
    for _ in range(max(1, int(samples))):
        started = time.perf_counter()
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "cold import failed")
        results.append(round(elapsed_ms, 3))
    return results


def build_report(*, samples: int, wheel: Path | None = None) -> dict[str, object]:
    cold_samples = measure_cold_start(samples)
    dependencies = read_direct_dependencies()
    ordered = sorted(cold_samples)
    p95_index = max(0, min(len(ordered) - 1, round(0.95 * len(ordered) + 0.5) - 1))
    return {
        "schema_version": 1,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cold_start": {
            "target": "import lightclaw_cli",
            "samples_ms": cold_samples,
            "median_ms": round(statistics.median(cold_samples), 3),
            "p95_ms": ordered[p95_index],
        },
        "dependencies": {
            "direct_count": len(dependencies),
            "direct": dependencies,
        },
        "wheel": {
            "path": wheel.name if wheel is not None else None,
            "bytes": wheel.stat().st_size if wheel is not None else None,
        },
    }


def validate_report(report: dict[str, object]) -> list[str]:
    budget = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))["limits"]
    errors: list[str] = []
    cold = report["cold_start"]
    dependencies = report["dependencies"]
    wheel = report["wheel"]
    if float(cold["p95_ms"]) > float(budget["cold_start_p95_ms"]):
        errors.append("cold start p95 exceeds the published budget")
    if int(dependencies["direct_count"]) > int(budget["direct_dependencies"]):
        errors.append("direct dependency count exceeds the published budget")
    if wheel["bytes"] is not None and int(wheel["bytes"]) > int(budget["wheel_bytes"]):
        errors.append("wheel size exceeds the published budget")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    wheel = args.wheel.resolve() if args.wheel else None
    if wheel is not None and not wheel.is_file():
        print(f"Wheel does not exist: {wheel}")
        return 2
    report = build_report(samples=max(1, min(20, args.samples)), wheel=wheel)
    errors = validate_report(report)
    report["budget_passed"] = not errors
    report["budget_errors"] = errors
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(
        f"Runtime footprint passed: p95={report['cold_start']['p95_ms']}ms, "
        f"dependencies={report['dependencies']['direct_count']}, "
        f"wheel_bytes={report['wheel']['bytes']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
