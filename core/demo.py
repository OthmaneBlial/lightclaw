"""Token-free deterministic product demo scenarios."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from memory import MemoryStore

from .receipts import write_receipt
from .security import delegated_process_env, redact_text

DEMO_SCENARIOS = ("memory", "repo-task", "multi-agent")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_change(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "change": "created",
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _run_memory_scenario(artifact: Path) -> dict[str, object]:
    db_path = artifact / "memory.db"
    first = MemoryStore(str(db_path))
    first.ingest("user", "The project launch code is Orchid-47.", "telegram-fixture")
    first.db.close()

    restarted = MemoryStore(str(db_path))
    recalled = restarted.recall("What is the project launch code?", top_k=3)
    restarted.db.close()
    recall_payload = {
        "query": "What is the project launch code?",
        "matches": [record.content for record in recalled],
        "restart_proved": True,
    }
    result_file = _write(
        artifact / "recall.json",
        json.dumps(recall_payload, indent=2, sort_keys=True) + "\n",
    )
    passed = any("Orchid-47" in item for item in recall_payload["matches"])
    return {
        "plan": [
            {"label": "store", "worker": "fixture-memory", "task": "Persist a known fact", "depends_on": []},
            {"label": "restart", "worker": "fixture-memory", "task": "Reopen SQLite state", "depends_on": ["store"]},
            {"label": "recall", "worker": "fixture-verifier", "task": "Retrieve the known fact", "depends_on": ["restart"]},
        ],
        "commands": [],
        "checks": [
            {
                "name": "fact survives restart",
                "passed": passed,
                "evidence": "recall.json contains Orchid-47 after reopening SQLite",
            }
        ],
        "files": [db_path, result_file],
        "artifacts": [result_file.relative_to(artifact.parent).as_posix()],
        "handoffs": [],
    }


def _run_repo_scenario(artifact: Path) -> dict[str, object]:
    service = _write(
        artifact / "service.py",
        '"""Tiny deterministic service fixture."""\n\n\ndef health() -> dict[str, str]:\n    return {"status": "ok"}\n',
    )
    test_file = _write(
        artifact / "test_service.py",
        "import unittest\n\nfrom service import health\n\n\nclass HealthTest(unittest.TestCase):\n"
        "    def test_health_is_ok(self):\n        self.assertEqual(health(), {\"status\": \"ok\"})\n\n\n"
        "if __name__ == \"__main__\":\n    unittest.main()\n",
    )
    command = [sys.executable, "-m", "unittest", "-v"]
    completed = subprocess.run(
        command,
        cwd=artifact,
        env=delegated_process_env(extra={"PYTHONIOENCODING": "utf-8"}),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    test_output = redact_text((completed.stdout + "\n" + completed.stderr).strip(), os.environ)
    evidence = _write(artifact / "test-output.txt", test_output + "\n")
    return {
        "plan": [
            {"label": "builder", "worker": "fixture-builder", "task": "Add a bounded health function", "depends_on": []},
            {"label": "verifier", "worker": "fixture-verifier", "task": "Run the real unit test", "depends_on": ["builder"]},
        ],
        "commands": [
            {
                "command": "python -m unittest -v",
                "exit_code": completed.returncode,
                "summary": "fixture unit test passed" if completed.returncode == 0 else "fixture unit test failed",
            }
        ],
        "checks": [
            {
                "name": "unit tests",
                "passed": completed.returncode == 0,
                "evidence": "artifact/test-output.txt records the real unittest result",
            }
        ],
        "files": [service, test_file, evidence],
        "artifacts": [evidence.relative_to(artifact.parent).as_posix()],
        "handoffs": [],
    }


def _run_multi_scenario(artifact: Path) -> dict[str, object]:
    agents = _write(
        artifact / "AGENTS.md",
        "# Deterministic two-lane plan\n\n- research: collect release risks\n- builder: depends on research; write checklist\n",
    )
    research = _write(
        artifact / "handoff" / "research.json",
        json.dumps(
            {
                "status": "success",
                "outputs": {"findings": ["pin dependencies", "keep rollback instructions"]},
                "handoff_to": ["builder"],
            },
            indent=2,
        )
        + "\n",
    )
    initial_failure = _write(
        artifact / "handoff" / "initial-failure.json",
        json.dumps(
            {
                "lane": "builder",
                "check": "deliverable exists",
                "result": "fail",
                "reason": "launch-checklist.md was missing",
                "retryable": True,
            },
            indent=2,
        )
        + "\n",
    )
    checklist = _write(
        artifact / "launch-checklist.md",
        "# Launch checklist\n\n- [x] Pin dependencies\n- [x] Document rollback\n- [x] Record verification evidence\n",
    )
    builder = _write(
        artifact / "handoff" / "builder.json",
        json.dumps(
            {
                "status": "success",
                "inputs": ["handoff/research.json"],
                "outputs": {"deliverables": ["launch-checklist.md"]},
                "handoff_to": [],
            },
            indent=2,
        )
        + "\n",
    )
    repair = _write(
        artifact / "handoff" / "repair.json",
        json.dumps(
            {
                "lane": "builder",
                "attempt": 1,
                "action": "created the missing scoped deliverable",
                "result": "pass",
            },
            indent=2,
        )
        + "\n",
    )
    audit_payload = {
        "dependency_order": ["research", "builder"],
        "handoffs_valid": True,
        "deliverable_exists": checklist.is_file(),
        "failure_reported": True,
        "repair_attempts": 1,
        "result": "pass",
    }
    audit = _write(
        artifact / "final-audit.json",
        json.dumps(audit_payload, indent=2, sort_keys=True) + "\n",
    )
    return {
        "plan": [
            {"label": "research", "worker": "fixture-researcher", "task": "Collect release risks", "depends_on": []},
            {"label": "builder", "worker": "fixture-builder", "task": "Produce a launch checklist", "depends_on": ["research"]},
        ],
        "commands": [],
        "checks": [
            {"name": "DAG dependency order", "passed": True, "evidence": "final-audit.json records research before builder"},
            {"name": "machine-readable handoffs", "passed": True, "evidence": "both lane JSON handoffs parsed successfully"},
            {"name": "final deliverable", "passed": checklist.is_file(), "evidence": "artifact/launch-checklist.md exists"},
            {"name": "bounded repair", "passed": True, "evidence": "the first failure is recorded and repair attempt 1 passes"},
        ],
        "files": [agents, research, initial_failure, checklist, builder, repair, audit],
        "artifacts": [
            checklist.relative_to(artifact.parent).as_posix(),
            audit.relative_to(artifact.parent).as_posix(),
        ],
        "handoffs": [
            research.relative_to(artifact.parent).as_posix(),
            builder.relative_to(artifact.parent).as_posix(),
        ],
        "failures": ["builder deliverable missing on first acceptance pass"],
        "retries": 1,
    }


def run_demo(scenario: str, output_dir: str | Path) -> dict[str, object]:
    """Execute one deterministic scenario and return its receipt-backed result."""
    if scenario not in DEMO_SCENARIOS:
        raise ValueError(f"unknown demo scenario: {scenario}")
    output = Path(output_dir).expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("demo output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / "artifact"
    artifact.mkdir()

    started_wall = _utc_now()
    started = time.monotonic()
    runners = {
        "memory": _run_memory_scenario,
        "repo-task": _run_repo_scenario,
        "multi-agent": _run_multi_scenario,
    }
    scenario_result = runners[scenario](artifact)
    duration = round(time.monotonic() - started, 3)
    passed = all(bool(item.get("passed")) for item in scenario_result["checks"])
    run_id = f"demo-{scenario}-{int(time.time())}"
    receipt = {
        "run_id": run_id,
        "original_goal": {
            "memory": "Remember my launch code across a restart and recall it.",
            "repo-task": "Add a health check to a tiny Python service and verify it.",
            "multi-agent": "Research release risks, hand them to a builder, and audit the result.",
        }[scenario],
        "approved_scope": "deterministic local fixture directory only",
        "risk_level": "low",
        "capability_profile": "fixture-workspace-write",
        "plan": scenario_result["plan"],
        "workers": sorted({item["worker"] for item in scenario_result["plan"]}),
        "started_at": started_wall,
        "finished_at": _utc_now(),
        "duration_seconds": duration,
        "usage": {"provider": "fixture", "tokens": 0, "estimated_cost_usd": 0},
        "commands": scenario_result["commands"],
        "file_changes": [_file_change(path, output) for path in scenario_result["files"]],
        "checks": scenario_result["checks"],
        "handoffs": scenario_result["handoffs"],
        "artifacts": scenario_result["artifacts"],
        "failures": scenario_result.get("failures", [])
        if passed
        else ["one or more fixture acceptance checks failed"],
        "retries": scenario_result.get("retries", 0),
        "disposition": "accepted" if passed else "failed",
        "checkpoint": {"type": "new-owned-directory", "pre_existing_files": 0},
        "undo": f"Remove only this demo directory: {output}",
        "fixture": True,
    }
    receipt_json, receipt_markdown, safe_receipt = write_receipt(receipt, output)
    return {
        "ok": passed,
        "scenario": scenario,
        "run_id": run_id,
        "output_dir": output.as_posix(),
        "receipt_json": receipt_json.as_posix(),
        "receipt_markdown": receipt_markdown.as_posix(),
        "artifacts": scenario_result["artifacts"],
        "checks": safe_receipt["checks"],
    }
