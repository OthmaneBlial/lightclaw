from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from core.artifacts import (
    ArtifactError,
    accept_artifact,
    apply_selected_files,
    build_pull_request_preview,
    create_isolated_worktree,
    create_patch_bundle,
    initialize_artifact_repository,
    publish_pull_request,
    reject_artifact,
)
from core.receipts import write_receipt
from lightclaw_cli import build_parser


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", root.as_posix(), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _receipt(run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "original_goal": "Add a bounded health check",
        "approved_scope": "fixture repository only",
        "risk_level": "low",
        "capability_profile": "workspace-write",
        "plan": [],
        "started_at": "2026-08-23T10:00:00Z",
        "finished_at": "2026-08-23T10:00:01Z",
        "commands": [],
        "file_changes": [],
        "diff_summary": "1 file changed",
        "checks": [
            {"name": "unit tests", "passed": True, "evidence": "2 tests passed"}
        ],
        "handoffs": [],
        "artifacts": [],
        "failures": [],
        "retries": 0,
        "disposition": "ready_for_review",
        "checkpoint": {"type": "git-checkpoint"},
        "undo": "git reset --mixed HEAD",
    }


def test_patch_bundle_is_private_reproducible_and_locally_acceptable(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "service.py"
    source.write_text("VERSION = 1\n", encoding="utf-8")

    checkpoint = initialize_artifact_repository(workspace, "run-123")
    source.write_text("VERSION = 2\n", encoding="utf-8")
    (workspace / "test_service.py").write_text("assert True\n", encoding="utf-8")
    bundle = create_patch_bundle(workspace, tmp_path / "receipt", run_id="run-123")

    assert checkpoint["branch"] == "lightclaw/run-123"
    assert checkpoint["base_commit"] == _git(workspace, "rev-parse", "HEAD")
    assert bundle["published"] is False
    assert bundle["changed_paths"] == [
        {"path": "service.py", "status": "M"},
        {"path": "test_service.py", "status": "A"},
    ]
    patch = Path(str(bundle["patch"]))
    manifest = Path(str(bundle["manifest"]))
    assert "VERSION = 2" in patch.read_text(encoding="utf-8")
    assert stat.S_IMODE(patch.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", workspace.as_posix(), clone.as_posix()],
        check=True,
    )
    _git(clone, "apply", "--check", patch.as_posix())
    _git(clone, "apply", patch.as_posix())
    assert (clone / "service.py").read_text(encoding="utf-8") == "VERSION = 2\n"

    accepted = accept_artifact(workspace, "run-123")
    assert accepted["published"] is False
    assert _git(workspace, "log", "-1", "--pretty=%s") == "LightClaw accepted result run-123"
    assert _git(workspace, "remote") == ""


def test_reject_preserves_files_and_only_unstages(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initialize_artifact_repository(workspace, "run-reject")
    result_file = workspace / "result.txt"
    result_file.write_text("keep for review\n", encoding="utf-8")
    create_patch_bundle(workspace, tmp_path / "receipt", run_id="run-reject")

    rejected = reject_artifact(workspace, "run-reject")

    assert rejected["preserved"] is True
    assert result_file.read_text(encoding="utf-8") == "keep for review\n"
    assert _git(workspace, "diff", "--cached", "--name-only") == ""
    assert _git(workspace, "status", "--porcelain") == "?? result.txt"


def test_selective_apply_previews_backs_up_and_preserves_unrelated_work(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "src").mkdir()
    (target / "src").mkdir()
    (source / "src" / "chosen.py").write_text("new\n", encoding="utf-8")
    (source / "created.txt").write_text("created\n", encoding="utf-8")
    (target / "src" / "chosen.py").write_text("old\n", encoding="utf-8")
    unrelated = target / "unrelated.txt"
    unrelated.write_text("user work\n", encoding="utf-8")

    preview = apply_selected_files(
        source,
        target,
        ["src/chosen.py", "created.txt"],
        run_id="run-apply",
    )
    assert preview["applied"] is False
    assert (target / "src" / "chosen.py").read_text(encoding="utf-8") == "old\n"
    assert not (target / "created.txt").exists()

    applied = apply_selected_files(
        source,
        target,
        ["src/chosen.py", "created.txt"],
        run_id="run-apply",
        apply=True,
    )
    assert applied["applied"] is True
    assert (target / "src" / "chosen.py").read_text(encoding="utf-8") == "new\n"
    assert (target / "created.txt").read_text(encoding="utf-8") == "created\n"
    assert (
        target / ".lightclaw-backups" / "run-apply" / "src" / "chosen.py"
    ).read_text(encoding="utf-8") == "old\n"
    assert unrelated.read_text(encoding="utf-8") == "user work\n"

    with pytest.raises(ArtifactError, match="safe and relative"):
        apply_selected_files(source, target, ["../unrelated.txt"], run_id="bad")
    (source / "linked.py").symlink_to(source / "src" / "chosen.py")
    with pytest.raises(ArtifactError, match="regular file"):
        apply_selected_files(source, target, ["linked.py"], run_id="bad")


def test_real_worktree_keeps_source_checkout_on_its_branch(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "fixture",
    )

    target = tmp_path / "worktree"
    created = create_isolated_worktree(source, target, "real-task")

    assert created["branch"] == "lightclaw/real-task"
    assert _git(source, "branch", "--show-current") == "main"
    assert _git(target, "branch", "--show-current") == "lightclaw/real-task"
    assert (target / "README.md").read_text(encoding="utf-8") == "fixture\n"


def test_pr_preview_contains_receipt_evidence_and_requires_exact_confirmation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initialize_artifact_repository(workspace, "run-pr")
    _git(workspace, "remote", "add", "origin", "https://example.invalid/lightclaw.git")
    (workspace / "health.py").write_text("STATUS = 'ok'\n", encoding="utf-8")
    create_patch_bundle(workspace, tmp_path / "artifact", run_id="run-pr")
    receipt_path, _, _ = write_receipt(_receipt("run-pr"), tmp_path / "receipt")

    unaccepted = build_pull_request_preview(
        workspace,
        receipt_path,
        run_id="run-pr",
        title="Add a health check",
    )
    assert unaccepted["ready_to_publish"] is False
    with pytest.raises(ArtifactError, match="accepted local artifact"):
        publish_pull_request(unaccepted, confirmation="run-pr")

    accept_artifact(workspace, "run-pr")
    preview = build_pull_request_preview(
        workspace,
        receipt_path,
        run_id="run-pr",
        title="Add a health check",
    )

    assert preview["published"] is False
    assert preview["ready_to_publish"] is True
    assert preview["commits_ahead"] == 1
    assert preview["requires_confirmation"] == "run-pr"
    assert "[PASS] unit tests: 2 tests passed" in str(preview["body"])
    assert "Diff: 1 file changed" in str(preview["body"])
    assert preview["commands"][0][:3] == ["git", "push", "--set-upstream"]
    with pytest.raises(ArtifactError, match="exact run id"):
        publish_pull_request(preview, confirmation="wrong-run")


def test_artifact_cli_defaults_to_preview_and_exposes_publish_confirmation():
    parser = build_parser()
    parsed = parser.parse_args(
        [
            "artifact",
            "pr",
            "run-123",
            "--base",
            "develop",
            "--confirm-publish",
            "run-123",
        ]
    )

    assert parsed.artifact_action == "pr"
    assert parsed.run_id == "run-123"
    assert parsed.base == "develop"
    assert parsed.confirm_publish == "run-123"
    assert parsed.apply is False
