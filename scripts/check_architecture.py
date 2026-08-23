#!/usr/bin/env python3
"""Measure and enforce LightClaw's explicit core complexity budget."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUDGET_PATH = PROJECT_ROOT / "docs" / "architecture" / "core-budget.json"
BOUNDARIES_PATH = PROJECT_ROOT / "docs" / "architecture" / "module-boundaries.json"
DEFAULT_REPORT = PROJECT_ROOT / "docs" / "generated" / "architecture-metrics.json"
TOP_LEVEL_MODULES = (
    "config.py",
    "lightclaw_cli.py",
    "main.py",
    "memory.py",
    "providers.py",
    "skills.py",
)
BRANCH_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.BoolOp,
    ast.Match,
    ast.IfExp,
)


def _load_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _runtime_paths() -> list[Path]:
    paths = list((PROJECT_ROOT / "core").rglob("*.py"))
    paths.extend(PROJECT_ROOT / name for name in TOP_LEVEL_MODULES)
    return sorted(path for path in paths if path.is_file())


def _direct_dependencies() -> list[str]:
    try:
        import tomllib
    except ModuleNotFoundError as exc:  # pragma: no cover - quality jobs use 3.11+
        raise RuntimeError("architecture check requires Python 3.11+") from exc
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project.get("project", {}).get("dependencies", [])
    return sorted(str(item) for item in dependencies)


def build_report() -> dict[str, object]:
    module_rows: list[dict[str, object]] = []
    function_rows: list[dict[str, object]] = []
    total_lines = 0
    for path in _runtime_paths():
        source = path.read_text(encoding="utf-8")
        line_count = len(source.splitlines())
        total_lines += line_count
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        module_rows.append({"path": relative, "lines": line_count})
        tree = ast.parse(source, filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            function_rows.append(
                {
                    "path": relative,
                    "name": node.name,
                    "line": node.lineno,
                    "lines": int(node.end_lineno or node.lineno) - node.lineno + 1,
                    "branch_points": sum(
                        isinstance(child, BRANCH_NODES) for child in ast.walk(node)
                    ),
                }
            )
    dependencies = _direct_dependencies()
    return {
        "schema_version": 1,
        "runtime_lines": total_lines,
        "direct_dependencies": dependencies,
        "direct_dependency_count": len(dependencies),
        "largest_modules": sorted(
            module_rows,
            key=lambda row: (-int(row["lines"]), str(row["path"])),
        )[:15],
        "largest_functions": sorted(
            function_rows,
            key=lambda row: (
                -int(row["lines"]),
                -int(row["branch_points"]),
                str(row["path"]),
                str(row["name"]),
            ),
        )[:15],
        "highest_branch_functions": sorted(
            function_rows,
            key=lambda row: (
                -int(row["branch_points"]),
                -int(row["lines"]),
                str(row["path"]),
                str(row["name"]),
            ),
        )[:15],
    }


def validate_report(report: dict[str, object]) -> list[str]:
    budget = _load_object(BUDGET_PATH)
    limits = budget.get("limits")
    if not isinstance(limits, dict):
        return ["architecture budget has no limits object"]
    errors: list[str] = []
    if int(report["runtime_lines"]) > int(limits["runtime_lines"]):
        errors.append("runtime line budget exceeded")
    if int(report["direct_dependency_count"]) > int(limits["direct_dependencies"]):
        errors.append("direct dependency budget exceeded")
    for row in report["largest_modules"]:
        ceiling = (
            int(limits["core_module_lines"])
            if str(row["path"]).startswith("core/")
            else int(limits["runtime_module_lines"])
        )
        if int(row["lines"]) > ceiling:
            errors.append(f"module line budget exceeded: {row['path']} ({row['lines']}>{ceiling})")
    for row in report["largest_functions"]:
        if int(row["lines"]) > int(limits["function_lines"]):
            errors.append(
                f"function line budget exceeded: {row['path']}::{row['name']}"
            )
    for row in report["highest_branch_functions"]:
        if int(row["branch_points"]) > int(limits["function_branch_points"]):
            errors.append(
                f"function branch budget exceeded: {row['path']}::{row['name']}"
            )

    boundaries = _load_object(BOUNDARIES_PATH)
    required = set(str(item) for item in boundaries.get("required_concerns", []))
    modules = boundaries.get("modules")
    invariants = boundaries.get("invariants")
    if not isinstance(modules, dict) or not isinstance(invariants, dict):
        return errors + ["module boundary contract is malformed"]
    owners: dict[str, set[str]] = {concern: set() for concern in required}
    max_concerns = int(invariants.get("max_concerns_per_module", 3))
    for path, raw_concerns in modules.items():
        if not (PROJECT_ROOT / str(path)).is_file():
            errors.append(f"boundary owner is missing: {path}")
            continue
        concerns = {str(item) for item in raw_concerns} if isinstance(raw_concerns, list) else set()
        if len(concerns) > max_concerns:
            errors.append(f"module owns too many orchestration concerns: {path}")
        for concern in concerns:
            owners.setdefault(concern, set()).add(str(path))
    for concern in sorted(required):
        if not owners.get(concern):
            errors.append(f"orchestration concern has no owner: {concern}")
    if owners.get("planning", set()) & owners.get("acceptance", set()):
        errors.append("planning and acceptance must have separate module owners")
    if owners.get("execution", set()) & owners.get("acceptance", set()):
        errors.append("execution and acceptance must have separate module owners")
    return errors


def _report_text(report: dict[str, object]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check budget and committed report")
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    report = build_report()
    errors = validate_report(report)
    expected = _report_text(report)
    if args.check and (
        not args.output.is_file() or args.output.read_text(encoding="utf-8") != expected
    ):
        errors.append(f"generated architecture report is stale: {args.output}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    if not args.check:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(expected, encoding="utf-8")
    print(
        f"Architecture budget passed: {report['runtime_lines']} lines, "
        f"{report['direct_dependency_count']} direct dependencies."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
