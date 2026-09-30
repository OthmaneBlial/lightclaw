from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import artifacts as artifact_module
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
from lightclaw_cli import build_parser, cmd_artifact


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", root.as_posix(), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_task_checkpoint_does_not_commit_or_switch_its_parent_repository(tmp_path):
    parent = tmp_path / "source-repository"
    parent.mkdir()
    _git(parent, "init", "-b", "main")
    _git(parent, "config", "user.name", "Fixture")
    _git(parent, "config", "user.email", "fixture@local.invalid")
    source = parent / "source.py"
    source.write_text("original source\n")
    _git(parent, "add", "source.py")
    _git(parent, "commit", "-m", "source checkpoint")
    source.write_text("staged user changes\n")
    _git(parent, "add", "source.py")
    (parent / "private.txt").write_text("unrelated untracked content")
    workspace = parent / "owned-task"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("approved task plan")
    parent_head = _git(parent, "rev-parse", "HEAD")
    parent_status = _git(parent, "status", "--porcelain")
    parent_index = (parent / ".git" / "index").read_bytes()

    checkpoint = initialize_artifact_repository(workspace, "nested-task")

    assert _git(parent, "rev-parse", "HEAD") == parent_head
    assert (parent / ".git" / "index").read_bytes() == parent_index
    assert _git(parent, "branch", "--show-current") == "main"
    assert _git(parent, "status", "--porcelain") == parent_status
    assert Path(_git(workspace, "rev-parse", "--show-toplevel")) == workspace.resolve()
    assert _git(workspace, "ls-files") == "AGENTS.md"
    assert checkpoint["branch"] == "lightclaw/nested-task"


def test_checkpoint_refuses_workspace_swapped_after_directory_check(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "private.txt").write_text("preserve outside content")
    is_dir = Path.is_dir

    def swap_after_check(path):
        result = is_dir(path)
        if path == workspace:
            workspace.rename(tmp_path / "original-workspace")
            workspace.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(Path, "is_dir", swap_after_check)
    try:
        initialize_artifact_repository(workspace, "swapped-checkpoint")
    except ArtifactError:
        pass
    assert not (outside / ".git").exists()
    assert (outside / "private.txt").read_text() == "preserve outside content"


@pytest.mark.parametrize("operation", ["bundle", "accept", "reject", "preview"])
@pytest.mark.parametrize("workspace_kind", ["nested", "symlink"])
def test_artifact_operations_cannot_use_another_repository(tmp_path, operation, workspace_kind):
    parent = tmp_path / "source"
    parent.mkdir()
    (parent / "source.py").write_text("original\n")
    initialize_artifact_repository(parent, "source")
    _git(parent, "remote", "add", "origin", "https://example.invalid/source.git")
    (parent / "source.py").write_text("staged user changes\n")
    _git(parent, "add", "source.py")
    workspace = parent / "task" if workspace_kind == "nested" else tmp_path / "alias"
    if workspace_kind == "nested":
        workspace.mkdir()
        (workspace / "result.py").write_text("agent output\n")
    else:
        workspace.symlink_to(parent, target_is_directory=True)
    receipt, _, _ = write_receipt(_receipt("task"), tmp_path / "receipts")
    head = _git(parent, "rev-parse", "HEAD")
    branch = _git(parent, "branch", "--show-current")
    index = (parent / ".git" / "index").read_bytes()
    actions = {
        "bundle": lambda: create_patch_bundle(workspace, tmp_path / "bundle", run_id="task"),
        "accept": lambda: accept_artifact(workspace, "task"),
        "reject": lambda: reject_artifact(workspace, "task"),
        "preview": lambda: build_pull_request_preview(
            workspace, receipt, run_id="task", title="Review task"
        ),
    }

    with pytest.raises(ArtifactError):
        actions[operation]()

    assert _git(parent, "rev-parse", "HEAD") == head
    assert _git(parent, "branch", "--show-current") == branch
    assert (parent / ".git" / "index").read_bytes() == index
    assert (parent / "source.py").read_text() == "staged user changes\n"
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("phase", ["before_open", "launch"])
def test_artifact_git_cannot_stage_files_in_replaced_workspace(tmp_path, monkeypatch, phase):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    for root in (workspace, outside):
        _git(root, "init", "-b", "main")
    (workspace / "inside.txt").write_text("approved")
    (outside / "outside.txt").write_text("private")
    run = subprocess.run

    def swap():
        workspace.rename(tmp_path / "original")
        workspace.symlink_to(outside, target_is_directory=True)

    def swapped_launch(*args, **kwargs):
        swap()
        return run(*args, **kwargs)

    if phase == "before_open":
        swap()
        with pytest.raises(ArtifactError):
            artifact_module._git(workspace, "add", "-A")
    else:
        monkeypatch.setattr(subprocess, "run", swapped_launch)
        result = artifact_module._git(workspace, "add", "-A")
        monkeypatch.setattr(subprocess, "run", run)
        assert result.returncode == 0
    assert _git(outside, "diff", "--cached", "--name-only") == ""
    if phase == "launch":
        assert _git(tmp_path / "original", "diff", "--cached", "--name-only") == "inside.txt"


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
    assert json.loads(manifest.read_text(encoding="utf-8"))["patch_sha256"] == hashlib.sha256(
        patch.read_bytes()
    ).hexdigest()
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


def test_patch_bundle_preserves_unusual_git_paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initialize_artifact_repository(workspace, "run-paths")
    unusual_path = "line\nbreak "
    (workspace / unusual_path).write_text("review this path\n", encoding="utf-8")

    bundle = create_patch_bundle(workspace, tmp_path / "receipt", run_id="run-paths")

    assert bundle["changed_paths"] == [{"status": "A", "path": unusual_path}]


def test_patch_bundle_refuses_more_than_500_changed_paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initialize_artifact_repository(workspace, "run-many-paths")
    for index in range(501):
        (workspace / f"file-{index:03}.txt").touch()

    with pytest.raises(ArtifactError, match="at most 500 changed paths"):
        create_patch_bundle(workspace, tmp_path / "receipt", run_id="run-many-paths")


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
    (source / "src" / "chosen.py").chmod(0o751)
    (target / "src" / "chosen.py").chmod(0o640)
    unrelated = target / "unrelated.txt"
    unrelated.write_text("user work\n", encoding="utf-8")

    preview = apply_selected_files(
        source,
        target,
        ["src/chosen.py", "created.txt"],
        run_id="run-apply",
    )
    assert preview["applied"] is False
    assert preview["operations"][0]["target_sha256"] == hashlib.sha256(b"old\n").hexdigest()
    assert preview["operations"][0]["source_mode"] == "0751"
    assert preview["operations"][0]["target_mode"] == "0640"
    assert len(str(preview["plan_sha256"])) == 64
    assert (target / "src" / "chosen.py").read_text(encoding="utf-8") == "old\n"
    assert not (target / "created.txt").exists()

    applied = apply_selected_files(
        source,
        target,
        ["src/chosen.py", "created.txt"],
        run_id="run-apply",
        apply=True,
        confirm_plan=preview["plan_sha256"],
    )
    assert applied["applied"] is True
    assert (target / "src" / "chosen.py").read_text(encoding="utf-8") == "new\n"
    assert stat.S_IMODE((target / "src" / "chosen.py").stat().st_mode) == 0o751
    assert (target / "created.txt").read_text(encoding="utf-8") == "created\n"
    backup = target / ".lightclaw-backups" / "run-apply" / "src" / "chosen.py"
    assert backup.read_text(encoding="utf-8") == "old\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o640
    assert stat.S_IMODE((target / ".lightclaw-backups").stat().st_mode) == 0o700
    assert unrelated.read_text(encoding="utf-8") == "user work\n"

    with pytest.raises(ArtifactError, match="safe and relative"):
        apply_selected_files(source, target, ["../unrelated.txt"], run_id="bad")
    (source / "linked.py").symlink_to(source / "src" / "chosen.py")
    with pytest.raises(ArtifactError, match="symlink"):
        apply_selected_files(source, target, ["linked.py"], run_id="bad")


@pytest.mark.parametrize("changed", ["source", "target", "source_mode", "target_mode"])
def test_selective_apply_rejects_a_plan_changed_after_preview(tmp_path, changed):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "selected.txt").write_text("approved source\n", encoding="utf-8")
    (target / "selected.txt").write_text("approved target\n", encoding="utf-8")
    (source / "selected.txt").chmod(0o644)
    (target / "selected.txt").chmod(0o644)
    preview = apply_selected_files(source, target, ["selected.txt"], run_id="run-stale")

    if changed == "source":
        (source / "selected.txt").write_text("changed after preview\n", encoding="utf-8")
    elif changed == "target":
        (target / "selected.txt").write_text("changed after preview\n", encoding="utf-8")
    elif changed == "source_mode":
        (source / "selected.txt").chmod(0o755)
    else:
        (target / "selected.txt").chmod(0o600)

    with pytest.raises(ArtifactError, match="plan changed"):
        apply_selected_files(
            source,
            target,
            ["selected.txt"],
            run_id="run-stale",
            apply=True,
            confirm_plan=preview["plan_sha256"],
        )

    expected_target = "changed after preview\n" if changed == "target" else "approved target\n"
    assert (target / "selected.txt").read_text(encoding="utf-8") == expected_target
    assert not (target / ".lightclaw-backups" / "run-stale" / "selected.txt").exists()


def test_selective_apply_requires_the_preview_plan_hash(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "selected.txt").write_text("new\n", encoding="utf-8")

    with pytest.raises(ArtifactError, match="confirmation is missing"):
        apply_selected_files(source, target, ["selected.txt"], run_id="run-confirm", apply=True)

    assert not (target / "selected.txt").exists()


def test_selective_apply_does_not_overwrite_a_target_created_during_apply(tmp_path, monkeypatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "selected.txt").write_text("approved\n", encoding="utf-8")
    preview = apply_selected_files(source, target, ["selected.txt"], run_id="run-create-race")
    link = artifact_module.os.link

    def create_then_link(src, dst, **kwargs):
        (target / "selected.txt").write_text("concurrent user file\n", encoding="utf-8")
        return link(src, dst, **kwargs)

    monkeypatch.setattr("core.artifacts.os.link", create_then_link)
    with pytest.raises(ArtifactError, match="target appeared during apply"):
        apply_selected_files(
            source,
            target,
            ["selected.txt"],
            run_id="run-create-race",
            apply=True,
            confirm_plan=preview["plan_sha256"],
        )

    assert (target / "selected.txt").read_text(encoding="utf-8") == "concurrent user file\n"


def test_selective_apply_rejects_target_mutation_during_backup(tmp_path, monkeypatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "selected.txt").write_text("approved source\n", encoding="utf-8")
    target_file = target / "selected.txt"
    target_file.write_text("approved target\n", encoding="utf-8")
    preview = apply_selected_files(source, target, ["selected.txt"], run_id="run-backup-race")
    copy_to_temp = artifact_module._copy_to_temp

    def mutate_target(source_fd, directory_fd, name, source_stat):
        target_file.write_text("concurrent target edit\n", encoding="utf-8")
        return copy_to_temp(source_fd, directory_fd, name, source_stat)

    monkeypatch.setattr("core.artifacts._copy_to_temp", mutate_target)
    with pytest.raises(ArtifactError, match="target changed during apply"):
        apply_selected_files(
            source,
            target,
            ["selected.txt"],
            run_id="run-backup-race",
            apply=True,
            confirm_plan=preview["plan_sha256"],
        )

    assert target_file.read_text(encoding="utf-8") == "concurrent target edit\n"
    assert not (target / ".lightclaw-backups" / "run-backup-race" / "selected.txt").exists()


@pytest.mark.parametrize("change", ["content", "mode", "replacement", "symlink"])
def test_selective_apply_preserves_target_changed_after_backup(tmp_path, monkeypatch, change):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "selected.txt").write_text("approved agent result\n")
    selected = target / "selected.txt"
    selected.write_text("original human file\n")
    selected.chmod(0o640)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside private file\n")
    preview = apply_selected_files(source, target, ["selected.txt"], run_id="late-edit")
    copy_to_temp = artifact_module._copy_to_temp
    selected_copies = 0

    def change_after_final_copy(source_fd, directory_fd, name, source_stat):
        nonlocal selected_copies
        result = copy_to_temp(source_fd, directory_fd, name, source_stat)
        if name == "selected.txt":
            selected_copies += 1
            if selected_copies == 2:
                if change == "content":
                    selected.write_text("concurrent human edit\n")
                elif change == "mode":
                    selected.chmod(0o600)
                elif change == "replacement":
                    replacement = target / "editor-save.txt"
                    replacement.write_text("concurrent human edit\n")
                    replacement.replace(selected)
                else:
                    selected.unlink()
                    selected.symlink_to(outside)
        return result

    monkeypatch.setattr(artifact_module, "_copy_to_temp", change_after_final_copy)
    with pytest.raises(ArtifactError, match="target.*(changed|symlink)"):
        apply_selected_files(
            source, target, ["selected.txt"], run_id="late-edit",
            apply=True, confirm_plan=preview["plan_sha256"],
        )
    assert selected_copies == 2
    assert (target / ".lightclaw-backups" / "late-edit" / "selected.txt").read_text() == "original human file\n"
    assert not list(target.glob(".selected.txt.*.tmp"))
    assert outside.read_text() == "outside private file\n"
    if change == "symlink":
        assert selected.is_symlink()
    elif change == "mode":
        assert stat.S_IMODE(selected.stat().st_mode) == 0o600
        assert selected.read_text() == "original human file\n"
    else:
        assert selected.read_text() == "concurrent human edit\n"


def test_selective_apply_preserves_target_replaced_while_final_hash_is_read(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "selected.txt").write_text("agent result\n")
    selected = target / "selected.txt"
    selected.write_text("original human file\n")
    preview = apply_selected_files(source, target, ["selected.txt"], run_id="hash-race")
    open_file, hash_fd = artifact_module._open_workspace_file, artifact_module._hash_fd
    final_target_fd = None

    def track_final_target(root, relative, label):
        nonlocal final_target_fd
        opened = open_file(root, relative, label)
        if isinstance(root, int) and label == "target path":
            final_target_fd = opened[0]
        return opened

    def replace_after_hash(file_fd):
        digest = hash_fd(file_fd)
        if file_fd == final_target_fd:
            replacement = target / "editor-save.txt"
            replacement.write_text("concurrent human edit\n")
            replacement.replace(selected)
        return digest

    monkeypatch.setattr(artifact_module, "_open_workspace_file", track_final_target)
    monkeypatch.setattr(artifact_module, "_hash_fd", replace_after_hash)
    with pytest.raises(ArtifactError, match="target changed during apply"):
        apply_selected_files(
            source, target, ["selected.txt"], run_id="hash-race",
            apply=True, confirm_plan=preview["plan_sha256"],
        )
    assert selected.read_text() == "concurrent human edit\n"
    assert not list(target.glob(".selected.txt.*.tmp"))


def test_selective_apply_rejects_target_permission_change_during_backup(tmp_path, monkeypatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "selected.txt").write_text("approved source\n", encoding="utf-8")
    target_file = target / "selected.txt"
    target_file.write_text("approved target\n", encoding="utf-8")
    target_file.chmod(0o640)
    preview = apply_selected_files(source, target, ["selected.txt"], run_id="run-mode-race")
    open_file = artifact_module._open_workspace_file
    target_opens = 0

    def change_target_mode(root, relative, label):
        nonlocal target_opens
        if label == "target path":
            target_opens += 1
            if target_opens == 2:
                target_file.chmod(0o600)
        return open_file(root, relative, label)

    monkeypatch.setattr("core.artifacts._open_workspace_file", change_target_mode)
    with pytest.raises(ArtifactError, match="target permissions changed during apply"):
        apply_selected_files(
            source,
            target,
            ["selected.txt"],
            run_id="run-mode-race",
            apply=True,
            confirm_plan=preview["plan_sha256"],
        )

    assert target_file.read_text(encoding="utf-8") == "approved target\n"
    assert not (target / ".lightclaw-backups" / "run-mode-race" / "selected.txt").exists()


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_selective_apply_cleans_earlier_backups_if_a_later_backup_fails(
    tmp_path, monkeypatch, cleanup_fails
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    for name in ("first.txt", "second.txt"):
        (source / name).write_text(f"approved {name}\n", encoding="utf-8")
        (target / name).write_text(f"original {name}\n", encoding="utf-8")
    selected = ["first.txt", "second.txt"]
    preview = apply_selected_files(source, target, selected, run_id="run-backup-cleanup")
    open_file = artifact_module._open_workspace_file
    target_opens = 0

    def change_second_target(root, relative, label):
        nonlocal target_opens
        if label == "target path":
            target_opens += 1
            if target_opens == 4:
                (target / "second.txt").write_text("changed during apply\n", encoding="utf-8")
        return open_file(root, relative, label)

    monkeypatch.setattr("core.artifacts._open_workspace_file", change_second_target)
    if cleanup_fails:
        open_directory = artifact_module._open_workspace_directory

        def fail_backup_cleanup(root, parts, *, label, create=False, private=False):
            if label == "backup cleanup":
                raise ArtifactError("simulated cleanup failure")
            return open_directory(root, parts, label=label, create=create, private=private)

        monkeypatch.setattr("core.artifacts._open_workspace_directory", fail_backup_cleanup)

    error = "cleanup was incomplete" if cleanup_fails else "target changed during apply"
    with pytest.raises(ArtifactError, match=error):
        apply_selected_files(
            source,
            target,
            selected,
            run_id="run-backup-cleanup",
            apply=True,
            confirm_plan=preview["plan_sha256"],
        )

    assert (target / "first.txt").read_text(encoding="utf-8") == "original first.txt\n"
    assert (target / "second.txt").read_text(encoding="utf-8") == "changed during apply\n"
    backup_dir = target / ".lightclaw-backups" / "run-backup-cleanup"
    assert bool(list(backup_dir.rglob("*.txt"))) is cleanup_fails


def test_selective_apply_rejects_source_symlink_replaced_after_validation(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    source_file = source / "selected.txt"
    source_file.write_text("approved\n", encoding="utf-8")
    preview = apply_selected_files(source, target, ["selected.txt"], run_id="run-race")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret\n", encoding="utf-8")
    validate = artifact_module._validate_workspace_path
    swapped = False

    def validate_then_swap(root, relative, label):
        nonlocal swapped
        validate(root, relative, label)
        if label == "selected source" and not swapped:
            swapped = True
            source_file.unlink()
            source_file.symlink_to(outside)

    monkeypatch.setattr("core.artifacts._validate_workspace_path", validate_then_swap)
    with pytest.raises(ArtifactError, match="symlink"):
        apply_selected_files(
            source,
            target,
            ["selected.txt"],
            run_id="run-race",
            apply=True,
            confirm_plan=preview["plan_sha256"],
        )

    assert not (target / "selected.txt").exists()
    assert outside.read_text(encoding="utf-8") == "outside secret\n"


def test_selective_apply_rejects_target_parent_symlink_replaced_after_validation(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    source.mkdir()
    target.mkdir()
    outside.mkdir()
    (source / "nested").mkdir()
    (target / "nested").mkdir()
    (source / "nested" / "selected.txt").write_text("new\n", encoding="utf-8")
    preview = apply_selected_files(
        source, target, ["nested/selected.txt"], run_id="run-race"
    )
    validate = artifact_module._validate_workspace_path
    swapped = False

    def validate_then_swap(root, relative, label):
        nonlocal swapped
        validate(root, relative, label)
        if label == "target path" and not swapped:
            swapped = True
            (target / "nested").rmdir()
            (target / "nested").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr("core.artifacts._validate_workspace_path", validate_then_swap)
    with pytest.raises(ArtifactError, match="symlink"):
        apply_selected_files(
            source,
            target,
            ["nested/selected.txt"],
            run_id="run-race",
            apply=True,
            confirm_plan=preview["plan_sha256"],
        )

    assert not (outside / "selected.txt").exists()


@pytest.mark.parametrize(
    ("change", "error"),
    [("content", "changed during apply"), ("mode", "permissions changed during apply")],
)
def test_selective_apply_rejects_source_changed_after_preflight(tmp_path, monkeypatch, change, error):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    source_file = source / "selected.txt"
    source_file.write_text("reviewed\n", encoding="utf-8")
    preview = apply_selected_files(source, target, ["selected.txt"], run_id="run-race")
    open_file = artifact_module._open_workspace_file
    selected_opens = 0

    def change_before_copy(root, relative, label):
        nonlocal selected_opens
        if label == "selected source":
            selected_opens += 1
            if selected_opens == 2:
                if change == "content":
                    source_file.write_text("changed after preview\n", encoding="utf-8")
                else:
                    source_file.chmod(0o755)
        return open_file(root, relative, label)

    monkeypatch.setattr("core.artifacts._open_workspace_file", change_before_copy)
    with pytest.raises(ArtifactError, match=error):
        apply_selected_files(
            source,
            target,
            ["selected.txt"],
            run_id="run-race",
            apply=True,
            confirm_plan=preview["plan_sha256"],
        )

    assert not (target / "selected.txt").exists()
    if change == "content":
        assert source_file.read_text(encoding="utf-8") == "changed after preview\n"


def test_selective_apply_validates_all_sources_before_replacing_any_target(tmp_path, monkeypatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    for name in ("first.txt", "second.txt"):
        (source / name).write_text(f"approved {name}\n", encoding="utf-8")
        (target / name).write_text(f"original {name}\n", encoding="utf-8")
    selected = ["first.txt", "second.txt"]
    preview = apply_selected_files(source, target, selected, run_id="run-multi-race")
    open_file = artifact_module._open_workspace_file
    selected_opens = 0

    def change_second_before_stage(root, relative, label):
        nonlocal selected_opens
        if label == "selected source":
            selected_opens += 1
            if selected_opens == 4:
                (source / "second.txt").write_text("changed during apply\n", encoding="utf-8")
        return open_file(root, relative, label)

    monkeypatch.setattr("core.artifacts._open_workspace_file", change_second_before_stage)
    with pytest.raises(ArtifactError, match="changed during apply"):
        apply_selected_files(
            source,
            target,
            selected,
            run_id="run-multi-race",
            apply=True,
            confirm_plan=preview["plan_sha256"],
        )

    assert (target / "first.txt").read_text(encoding="utf-8") == "original first.txt\n"
    assert (target / "second.txt").read_text(encoding="utf-8") == "original second.txt\n"
    assert not (target / ".lightclaw-backups" / "run-multi-race").exists()


def test_selective_apply_preflights_all_backup_paths_before_mutating(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    source.mkdir()
    target.mkdir()
    outside.mkdir()
    (source / "first.txt").write_text("new first\n", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "second.txt").write_text("new second\n", encoding="utf-8")
    (target / "first.txt").write_text("old first\n", encoding="utf-8")
    (target / "nested").mkdir()
    (target / "nested" / "second.txt").write_text("old second\n", encoding="utf-8")
    (outside / "second.txt").write_text("outside\n", encoding="utf-8")
    backup_run = target / ".lightclaw-backups" / "run-symlink"
    backup_run.mkdir(parents=True)
    (backup_run / "nested").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactError, match="backup path contains a symlink"):
        apply_selected_files(
            source,
            target,
            ["first.txt", "nested/second.txt"],
            run_id="run-symlink",
            apply=True,
        )

    assert (target / "first.txt").read_text(encoding="utf-8") == "old first\n"
    assert (target / "nested" / "second.txt").read_text(encoding="utf-8") == "old second\n"
    assert (outside / "second.txt").read_text(encoding="utf-8") == "outside\n"
    assert not (backup_run / "first.txt").exists()


def test_selective_apply_never_overwrites_an_existing_backup(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "file.txt").write_text("new\n", encoding="utf-8")
    (target / "file.txt").write_text("current target\n", encoding="utf-8")
    backup = target / ".lightclaw-backups" / "run-repeat" / "file.txt"
    backup.parent.mkdir(parents=True)
    backup.write_text("original backup\n", encoding="utf-8")

    with pytest.raises(ArtifactError, match="backup already exists"):
        apply_selected_files(
            source,
            target,
            ["file.txt"],
            run_id="run-repeat",
            apply=True,
        )

    assert (target / "file.txt").read_text(encoding="utf-8") == "current target\n"
    assert backup.read_text(encoding="utf-8") == "original backup\n"


def test_selective_apply_cleans_up_a_failed_backup_link(tmp_path, monkeypatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "file.txt").write_text("new\n", encoding="utf-8")
    (target / "file.txt").write_text("original target\n", encoding="utf-8")
    backup_parent = target / ".lightclaw-backups" / "run-failure"
    preview = apply_selected_files(source, target, ["file.txt"], run_id="run-failure")

    def fail_backup_link(*_args, **_kwargs):
        raise OSError("simulated backup link failure")

    monkeypatch.setattr("core.artifacts.os.link", fail_backup_link)
    with pytest.raises(OSError, match="simulated backup link failure"):
        apply_selected_files(
            source,
            target,
            ["file.txt"],
            run_id="run-failure",
            apply=True,
            confirm_plan=preview["plan_sha256"],
        )

    assert (target / "file.txt").read_text(encoding="utf-8") == "original target\n"
    assert not (backup_parent / "file.txt").exists()
    assert list(backup_parent.glob(".file.txt.*")) == []


def test_selective_apply_rejects_symlinked_workspace_roots(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "file.txt").write_text("source\n", encoding="utf-8")
    source_link = tmp_path / "source-link"
    target_link = tmp_path / "target-link"
    source_link.symlink_to(source, target_is_directory=True)
    target_link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ArtifactError, match="real directories"):
        apply_selected_files(source_link, target, ["file.txt"], run_id="root-link")
    with pytest.raises(ArtifactError, match="real directories"):
        apply_selected_files(source, target_link, ["file.txt"], run_id="root-link")


@pytest.mark.parametrize("side", ["source", "target"])
def test_selective_apply_refuses_root_swapped_after_directory_check(tmp_path, monkeypatch, side):
    source, target, outside = (tmp_path / name for name in ("source", "target", "outside"))
    for root in (source, target, outside):
        root.mkdir()
        (root / "selected.txt").write_text(f"{root.name} content\n")
    replaced = source if side == "source" else target
    is_dir = Path.is_dir

    def swap_after_check(path):
        result = is_dir(path)
        if path == replaced:
            replaced.rename(tmp_path / "original")
            replaced.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(Path, "is_dir", swap_after_check)
    with pytest.raises(ArtifactError, match="symlink"):
        apply_selected_files(source, target, ["selected.txt"], run_id="swapped-root")
    assert (outside / "selected.txt").read_text() == "outside content\n"
    assert not (outside / ".lightclaw-backups").exists()
    assert (tmp_path / "original" / "selected.txt").read_text() == f"{side} content\n"


def test_selective_apply_preserves_parent_directory_aliases(tmp_path):
    parent = tmp_path / "real"
    parent.mkdir()
    source, target = parent / "source", parent / "target"
    source.mkdir()
    target.mkdir()
    (source / "selected.txt").write_text("reviewed content\n")
    alias = tmp_path / "alias"
    alias.symlink_to(parent, target_is_directory=True)
    preview = apply_selected_files(alias / "source", alias / "target", ["selected.txt"], run_id="alias")
    applied = apply_selected_files(
        alias / "source", alias / "target", ["selected.txt"], run_id="alias",
        apply=True, confirm_plan=preview["plan_sha256"],
    )
    assert applied["applied"] is True
    assert (target / "selected.txt").read_text() == "reviewed content\n"


def test_artifact_checkpoint_rejects_symlinked_workspace_root(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    linked = tmp_path / "linked-workspace"
    linked.symlink_to(workspace, target_is_directory=True)

    with pytest.raises(ArtifactError, match="real directory"):
        initialize_artifact_repository(linked, "run-root-link")

    assert not (workspace / ".git").exists()


def test_selective_apply_hashes_large_source_without_read_bytes(tmp_path, monkeypatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    content = b"agent output\n" * 100_000
    source_file = source / "large.txt"
    source_file.write_bytes(content)
    expected_sha256 = hashlib.sha256(content).hexdigest()

    def fail_read_bytes(_path):
        raise AssertionError("unbounded Path.read_bytes call")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)
    preview = apply_selected_files(source, target, ["large.txt"], run_id="run-large")

    assert preview["operations"][0]["source_sha256"] == expected_sha256
    assert (source_file.stat().st_size > 1024 * 1024)


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
    source_head = _git(source, "rev-parse", "HEAD")
    created = create_isolated_worktree(source, target, "real-task")

    assert created["branch"] == "lightclaw/real-task"
    assert _git(source, "branch", "--show-current") == "main"
    assert _git(target, "branch", "--show-current") == "lightclaw/real-task"
    assert (target / "README.md").read_text(encoding="utf-8") == "fixture\n"
    (target / "README.md").write_text("reviewed worktree change\n", encoding="utf-8")
    bundle = create_patch_bundle(target, tmp_path / "review", run_id="real-task")
    accepted = accept_artifact(target, "real-task")
    assert bundle["changed_paths"] == [{"status": "M", "path": "README.md"}]
    assert accepted["commit"] != source_head
    assert _git(source, "rev-parse", "HEAD") == source_head
    assert (source / "README.md").read_text(encoding="utf-8") == "fixture\n"


def test_pr_preview_contains_receipt_evidence_and_requires_exact_confirmation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initialize_artifact_repository(workspace, "run-pr")
    _git(workspace, "remote", "add", "origin", "https://example.invalid/lightclaw.git")
    (workspace / "health.py").write_text("STATUS = 'ok'\n", encoding="utf-8")
    create_patch_bundle(workspace, tmp_path / "artifact", run_id="run-pr")
    receipt_path, _, _ = write_receipt(_receipt("run-pr"), tmp_path / "receipt")
    other_receipt_path, _, _ = write_receipt(
        _receipt("another-run"), tmp_path / "another-receipt"
    )

    with pytest.raises(ArtifactError, match="different run"):
        build_pull_request_preview(
            workspace,
            other_receipt_path,
            run_id="run-pr",
            title="Add a health check",
        )

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


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout"])
def test_pr_creation_keeps_body_private_and_preserves_outside_files(tmp_path, monkeypatch, outcome):
    import sys

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    for root in (workspace, outside):
        (root / ".git").mkdir(parents=True)
    sentinel = outside / ".git" / "lightclaw-pr-body.md"
    sentinel.write_text("unrelated private file")
    run = subprocess.run
    bodies = []
    permissions = []

    def fake_git(_root, *args, **kwargs):
        return ".git/lightclaw-pr-body.md" if args[0] == "rev-parse" else ""

    def fake_gh(command, **kwargs):
        if command[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        body = Path(command[command.index("--body-file") + 1])
        bodies.append(body)
        permissions.append((stat.S_IMODE(body.stat().st_mode), stat.S_IMODE(body.parent.stat().st_mode)))
        workspace.rename(tmp_path / "original")
        workspace.symlink_to(outside, target_is_directory=True)
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        program = (
            "import sys;from pathlib import Path;"
            "Path('marker').write_text(Path(sys.argv[1]).read_text());"
            "print('fixture rejected',file=sys.stderr) if int(sys.argv[2]) else print('https://example.invalid/pr/1');"
            "sys.exit(int(sys.argv[2]))"
        )
        local_command = [sys.executable, "-c", program, str(body), str(int(outcome == "failure"))]
        return run([*command[:command.index("gh")], *local_command], **kwargs)

    monkeypatch.setattr(artifact_module, "_require_git", fake_git)
    monkeypatch.setattr(subprocess, "run", fake_gh)
    preview = {
        "run_id": "fixture", "workspace": str(workspace), "branch": "fixture",
        "body": "reviewed body", "ready_to_publish": True,
    }
    if outcome != "success":
        error = "fixture rejected" if outcome == "failure" else "creation could not run"
        with pytest.raises(ArtifactError, match=error):
            publish_pull_request(preview, confirmation="fixture")
    else:
        result = publish_pull_request(preview, confirmation="fixture")
        assert result["url"] == "https://example.invalid/pr/1"
    assert sentinel.read_text() == "unrelated private file"
    assert not (outside / "marker").exists()
    marker = tmp_path / "original" / "marker"
    if outcome == "timeout":
        assert not marker.exists()
    else:
        assert marker.read_text() == "reviewed body"
    assert bodies and all(not body.exists() for body in bodies)
    assert all(file_mode & 0o077 == parent_mode & 0o077 == 0 for file_mode, parent_mode in permissions)


def test_pr_creation_rejects_replaced_workspace_before_push(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    workspace.symlink_to(outside, target_is_directory=True)

    def auth_only(command, **kwargs):
        assert command[:3] == ["gh", "auth", "status"], "replaced workspace must not run Git"
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", auth_only)
    with pytest.raises(ArtifactError, match="git command could not run"):
        publish_pull_request({
            "run_id": "fixture", "workspace": str(workspace), "branch": "fixture",
            "body": "reviewed body", "ready_to_publish": True,
        }, confirmation="fixture")


def test_pr_preview_rejects_oversized_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr("core.receipts.MAX_RECEIPT_BYTES", 8)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_bytes(b" " * 9)

    with pytest.raises(ArtifactError, match="private receipt is missing or invalid"):
        build_pull_request_preview(
            workspace,
            receipt_path,
            run_id="run-large-receipt",
            title="Review bounded receipt",
        )


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
    assert parsed.confirm_plan is None
    assert parsed.apply is False


@pytest.mark.parametrize("action", ["accept", "reject", "pr"])
def test_artifact_cli_refuses_a_symlinked_run_workspace(tmp_path, monkeypatch, capsys, action):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.py").write_text("original\n")
    initialize_artifact_repository(outside, "outside")
    _git(outside, "remote", "add", "origin", "https://example.invalid/source.git")
    (outside / "source.py").write_text("staged user changes\n")
    _git(outside, "add", "source.py")
    head = _git(outside, "rev-parse", "HEAD")
    index = (outside / ".git" / "index").read_bytes()
    workspace = tmp_path / "run"
    workspace.symlink_to(outside, target_is_directory=True)
    receipt, _, _ = write_receipt(_receipt("run"), tmp_path / "receipts")
    store = SimpleNamespace(
        get_job=lambda _run_id: {
            "workspace": str(workspace),
            "status": "accepted" if action == "pr" else "succeeded",
        },
        accept=Mock(), reject=Mock(), close=Mock(),
    )
    config = SimpleNamespace(
        memory_db_path=str(tmp_path / "memory.db"), workspace_path=str(tmp_path)
    )
    monkeypatch.setattr("config.load_config", lambda: config)
    monkeypatch.setattr("core.jobs.JobStore", lambda _path: store)
    argv = ["artifact", action, "run", "--home", str(tmp_path / "home"), "--receipt", str(receipt)]
    if action != "pr":
        argv.append("--apply")

    assert cmd_artifact(build_parser().parse_args(argv)) == 2
    assert "refused" in capsys.readouterr().out
    assert _git(outside, "rev-parse", "HEAD") == head
    assert (outside / ".git" / "index").read_bytes() == index
    assert (outside / "source.py").read_text() == "staged user changes\n"
    store.accept.assert_not_called()
    store.reject.assert_not_called()
    store.close.assert_called_once()


def test_artifact_cli_passes_the_confirmed_plan_to_selected_apply(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "target"
    config = SimpleNamespace(
        memory_db_path=str(tmp_path / "memory.db"), workspace_path=str(workspace)
    )

    class JobStoreStub:
        def __init__(self, _path):
            pass

        def get_job(self, _run_id):
            return {"workspace": str(workspace)}

        def close(self):
            pass

    apply = Mock(return_value={"applied": True, "plan_sha256": "a" * 64})
    monkeypatch.setenv("LIGHTCLAW_HOME", "")
    monkeypatch.setenv("LIGHTCLAW_CONFIG", "")
    monkeypatch.setattr("config.load_config", lambda: config)
    monkeypatch.setattr("core.jobs.JobStore", JobStoreStub)
    monkeypatch.setattr("core.artifacts.apply_selected_files", apply)
    args = build_parser().parse_args(
        [
            "artifact",
            "apply",
            "run-123",
            "--target",
            str(target),
            "--paths",
            "service.py",
            "--apply",
            "--confirm-plan",
            "a" * 64,
        ]
    )

    assert cmd_artifact(args) == 0
    assert '"applied": true' in capsys.readouterr().out
    apply.assert_called_once_with(
        workspace,
        str(target),
        ["service.py"],
        run_id="run-123",
        apply=True,
        confirm_plan="a" * 64,
    )


@pytest.mark.parametrize("apply", [False, True])
def test_artifact_pr_requires_durable_acceptance(tmp_path, monkeypatch, capsys, apply):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = SimpleNamespace(
        memory_db_path=str(tmp_path / "memory.db"), workspace_path=str(workspace)
    )

    class JobStoreStub:
        def __init__(self, _path):
            pass

        def get_job(self, _run_id):
            return {"workspace": str(workspace), "status": "succeeded"}

        def close(self):
            pass

    preview = Mock(return_value={"ready_to_publish": True, "run_id": "run-123"})
    publish = Mock(return_value={"published": True})
    monkeypatch.setenv("LIGHTCLAW_HOME", "")
    monkeypatch.setenv("LIGHTCLAW_CONFIG", "")
    monkeypatch.setattr("config.load_config", lambda: config)
    monkeypatch.setattr("core.jobs.JobStore", JobStoreStub)
    monkeypatch.setattr("core.artifacts.build_pull_request_preview", preview)
    monkeypatch.setattr("core.artifacts.publish_pull_request", publish)

    argv = ["artifact", "pr", "run-123", "--home", str(tmp_path / "home")]
    if apply:
        argv.extend(["--apply", "--confirm-publish", "run-123"])

    result = cmd_artifact(build_parser().parse_args(argv))

    assert result == 2
    assert "accepted local artifact" in capsys.readouterr().out
    preview.assert_not_called()
    publish.assert_not_called()
