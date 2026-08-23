from __future__ import annotations

import json

from core.demo import run_demo
from core.jobs import JobStore
from core.receipts import validate_receipt
from core.workspaces import register_task_workspace, undo_owned_task


def test_one_hundred_fixture_runs_have_valid_secret_safe_receipts(tmp_path, monkeypatch):
    secret = "phase2-reliability-private-value"
    monkeypatch.setenv("PHASE2_FIXTURE_SECRET", secret)

    for index in range(100):
        output = tmp_path / f"{secret}-{index:03d}"
        result = run_demo("memory", output)
        raw_json = (output / "receipt.json").read_text(encoding="utf-8")
        raw_markdown = (output / "receipt.md").read_text(encoding="utf-8")
        receipt = json.loads(raw_json)

        assert result["ok"] is True
        assert validate_receipt(receipt) == []
        assert receipt["run_id"].startswith("demo-memory-")
        assert secret not in raw_json
        assert secret not in raw_markdown
        assert "[REDACTED]" in raw_json


def test_crash_restart_cancel_and_undo_preserve_unrelated_user_work(tmp_path):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    owned = workspace_root / "20260823_120000_crash-fixture"
    owned.mkdir()
    register_task_workspace(workspace_root, owned, "crash fixture")
    partial = owned / "partial.txt"
    partial.write_text("recoverable partial output\n", encoding="utf-8")
    sibling = workspace_root / "user-project"
    sibling.mkdir()
    unrelated = sibling / "keep.txt"
    unrelated.write_text("unrelated user work\n", encoding="utf-8")

    database = tmp_path / "runtime" / "jobs.db"
    first = JobStore(database)
    job = first.create_job(
        workspace=owned,
        session_id="fixture",
        goal="bounded crash fixture",
        approved_scope="owned workspace only",
        risk_level="medium",
        capability_profile="workspace-write",
        plan=[
            {
                "label": "builder",
                "depends_on": [],
                "owned_paths": ["partial.txt"],
                "idempotent": False,
                "resumable": False,
            }
        ],
        status="queued",
    )
    first.claim_next(workspace=owned, worker_pid=999999)
    assert first.request_cancel(str(job["run_id"]))["status"] == "cancel_requested"
    first.close()  # Simulated process crash after durable cancellation request.

    restarted = JobStore(database)
    restored = restarted.get_job(str(job["run_id"]))
    assert restored["status"] == "cancel_requested"
    assert partial.read_text(encoding="utf-8") == "recoverable partial output\n"
    assert restarted.mark_canceled(str(job["run_id"]))["status"] == "canceled"
    restarted.close()

    preview = undo_owned_task(workspace_root, owned.name)
    assert preview["applied"] is False
    assert partial.is_file()
    assert unrelated.read_text(encoding="utf-8") == "unrelated user work\n"

    applied = undo_owned_task(workspace_root, owned.name, apply=True)
    assert applied["applied"] is True
    assert not owned.exists()
    assert unrelated.read_text(encoding="utf-8") == "unrelated user work\n"
