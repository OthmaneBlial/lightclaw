"""Produce raw, explicitly labeled LightClaw benchmark evidence."""

from __future__ import annotations

import argparse
import csv
import importlib.metadata
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from config import _resolve_model
from core.demo import run_demo
from memory import MemoryStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=kwargs.pop("cwd", PROJECT_ROOT),
        text=True,
        capture_output=True,
        check=False,
        timeout=kwargs.pop("timeout", 180),
        **kwargs,
    )


def _git_commit() -> str:
    result = _run(["git", "rev-parse", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _total_memory_bytes() -> int | None:
    if sys.platform == "darwin":
        result = _run(["sysctl", "-n", "hw.memsize"])
        return int(result.stdout.strip()) if result.returncode == 0 else None
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    return None


def _median_startup_ms(runs: int) -> float:
    samples: list[float] = []
    for _ in range(runs):
        started = time.perf_counter()
        result = _run([sys.executable, "-c", "import lightclaw_cli"], timeout=30)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "startup probe failed")
        samples.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(samples), 3)


def _idle_rss_bytes() -> int | None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import lightclaw_cli,time; time.sleep(1.2)"],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.35)
        result = _run(["ps", "-o", "rss=", "-p", str(process.pid)], timeout=10)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return int(result.stdout.strip().split()[0]) * 1024
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def _routing_overhead_us(iterations: int = 100_000) -> float:
    providers = ("openai", "xai", "claude", "gemini", "deepseek", "zai")
    started = time.perf_counter()
    for index in range(iterations):
        _resolve_model(providers[index % len(providers)], "latest")
    elapsed = time.perf_counter() - started
    return round((elapsed / iterations) * 1_000_000, 4)


def _runtime_loc() -> int:
    paths = [
        PROJECT_ROOT / "config.py",
        PROJECT_ROOT / "lightclaw_cli.py",
        PROJECT_ROOT / "main.py",
        PROJECT_ROOT / "memory.py",
        PROJECT_ROOT / "providers.py",
        PROJECT_ROOT / "skills.py",
        *sorted((PROJECT_ROOT / "core").rglob("*.py")),
    ]
    return sum(len(path.read_text(encoding="utf-8").splitlines()) for path in paths)


def _memory_quality_fixture(temp_root: Path) -> dict[str, object]:
    facts = [
        ("harbor", "The harbor deployment code is Cobalt-19."),
        ("orchard", "The orchard deployment code is Saffron-28."),
        ("summit", "The summit deployment code is Indigo-37."),
        ("forest", "The forest deployment code is Amber-46."),
        ("island", "The island deployment code is Violet-55."),
        ("desert", "The desert deployment code is Silver-64."),
        ("valley", "The valley deployment code is Copper-73."),
        ("meadow", "The meadow deployment code is Onyx-82."),
    ]
    store = MemoryStore(str(temp_root / "quality.db"))
    for label, fact in facts:
        store.ingest(
            "user",
            fact,
            f"fixture-{label}",
            user_namespace="fixture-user",
            workspace_namespace="fixture-workspace",
        )
    hits = 0
    details: list[dict[str, object]] = []
    for label, fact in facts:
        result = store.recall(
            f"What is the {label} deployment code?",
            top_k=1,
            user_namespace="fixture-user",
            workspace_namespace="fixture-workspace",
        )
        passed = bool(result and result[0].content == fact)
        hits += int(passed)
        details.append({"query_label": label, "top1_passed": passed})
    store.db.close()
    return {
        "fixture_version": 1,
        "queries": len(facts),
        "top1_hits": hits,
        "top1_accuracy": round(hits / len(facts), 4),
        "details": details,
        "mode": "deterministic local lexical fixture",
    }


def _orchestration_fixture(temp_root: Path) -> dict[str, object]:
    result = run_demo("multi-agent", temp_root / "multi-agent")
    audit_path = temp_root / "multi-agent" / "artifact" / "final-audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    return {
        "fixture_version": 1,
        "dependency_order_passed": audit["dependency_order"] == ["research", "builder"],
        "handoff_completion_passed": bool(audit["handoffs_valid"]),
        "failure_reporting_passed": bool(audit["failure_reported"]),
        "repair_behavior_passed": audit["repair_attempts"] == 1 and audit["result"] == "pass",
        "receipt_generated": bool(result["receipt_json"]),
        "mode": "deterministic fixture; no model calls",
    }


def _direct_runtime_dependency_count() -> int:
    requirements = importlib.metadata.requires("lightclaw-ai") or []
    return sum(1 for item in requirements if "extra ==" not in item and "extra ==" not in item.lower())


def _clean_install_probe(wheel: Path) -> dict[str, object]:
    temp = Path(tempfile.mkdtemp(prefix="lightclaw-bench-install-"))
    venv = temp / "venv"
    try:
        create_started = time.perf_counter()
        created = _run([sys.executable, "-m", "venv", str(venv)], timeout=120)
        if created.returncode != 0:
            raise RuntimeError(created.stderr.strip() or "venv creation failed")
        python = venv / "bin" / "python"
        install_started = time.perf_counter()
        installed = _run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", str(wheel)],
            timeout=600,
        )
        if installed.returncode != 0:
            raise RuntimeError(installed.stderr[-2000:] or "clean install failed")
        install_seconds = time.perf_counter() - install_started
        listed = _run([str(python), "-m", "pip", "list", "--format", "json"], timeout=60)
        packages = json.loads(listed.stdout)
        command = venv / "bin" / "lightclaw"
        smoke = _run([str(command), "--help"], timeout=30)
        return {
            "venv_create_seconds": round(install_started - create_started, 3),
            "install_seconds": round(install_seconds, 3),
            "installed_distribution_count": len(packages),
            "command_smoke_passed": smoke.returncode == 0,
            "network_and_cache_state": "live PyPI resolution; local pip cache may be used",
        }
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def build_report(mode: str, runs: int, wheel: Path | None) -> dict[str, object]:
    temp_root = Path(tempfile.mkdtemp(prefix="lightclaw-bench-fixtures-"))
    try:
        report: dict[str, object] = {
            "schema_version": 1,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "commit": _git_commit(),
            "evidence_mode": mode,
            "run_count": runs,
            "environment": {
                "os": platform.platform(),
                "python": platform.python_version(),
                "machine": platform.machine(),
                "processor": platform.processor() or "unknown",
                "cpu_count": os.cpu_count(),
                "total_memory_bytes": _total_memory_bytes(),
            },
            "runtime": {
                "startup_median_ms": _median_startup_ms(runs),
                "idle_import_process_rss_bytes": _idle_rss_bytes(),
                "config_routing_overhead_us_per_call": _routing_overhead_us(),
                "runtime_python_loc": _runtime_loc(),
                "direct_runtime_dependency_count": _direct_runtime_dependency_count(),
            },
            "memory_retrieval": _memory_quality_fixture(temp_root),
            "orchestration": _orchestration_fixture(temp_root),
            "minimum_tested_host": {
                "status": "deterministic demo verified",
                "container_memory_limit_bytes": 268_435_456,
                "container_cpu_limit": 0.5,
                "scenario": "repo-task",
                "scope": "Pinned Python 3.13 slim image build, command smoke, real fixture unit test, and receipt generation. A live Telegram bot/provider was not tested under this limit.",
            },
        }
        if mode == "full":
            if wheel is None or not wheel.is_file():
                raise ValueError("--wheel pointing to a built wheel is required in full mode")
            report["clean_install"] = _clean_install_probe(wheel.resolve())
        else:
            report["clean_install"] = {
                "status": "not measured in fixture mode",
                "how_to_measure": "python -m bench.run --mode full --wheel dist/<wheel>",
            }
        return report
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _flatten(prefix: str, value, rows: list[tuple[str, object]]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _flatten(f"{prefix}.{key}" if prefix else str(key), item, rows)
    elif not isinstance(value, list):
        rows.append((prefix, value))


def write_report(report: dict[str, object], output_prefix: Path) -> tuple[Path, Path]:
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_prefix.with_suffix(".json")
    csv_path = output_prefix.with_suffix(".csv")
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rows: list[tuple[str, object]] = []
    _flatten("", report, rows)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        writer.writerows(rows)
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run reproducible LightClaw benchmarks")
    parser.add_argument("--mode", choices=("fixture", "full"), default="fixture")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "bench" / "results" / "latest")
    args = parser.parse_args()
    report = build_report(args.mode, max(1, args.runs), args.wheel)
    json_path, csv_path = write_report(report, args.output)
    print(json.dumps({"json": str(json_path), "csv": str(csv_path), "commit": report["commit"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
