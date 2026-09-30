"""Reviewable Git artifacts, selective local apply, and explicit PR publishing."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from .fs import directory_command_at, open_directory_at, open_regular_file_at, sha256_file
from .receipts import _write_private, read_receipt
from .security import delegated_process_env, redact_text


class ArtifactError(ValueError):
    """Raised when an artifact operation cannot be proven safe."""


def _git(workspace: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        with directory_command_at(workspace, (), "git", "--git-dir=.git", "--work-tree=.", "-C", ".", *args) as (command, pass_fds):
            return subprocess.run(
                command,
                pass_fds=pass_fds,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=delegated_process_env(),
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArtifactError(f"git command could not run: {' '.join(args)}") from exc


def _require_git(workspace: Path, *args: str, timeout: int = 30) -> str:
    result = _git(workspace, *args, timeout=timeout)
    if result.returncode != 0:
        detail = redact_text(result.stderr or result.stdout).strip()[-800:]
        raise ArtifactError(detail or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _safe_branch(run_id: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(run_id)).strip("-.")[:80]
    return f"lightclaw/{slug or 'run'}"


def _safe_git_ref(raw: str, label: str) -> str:
    value = str(raw).strip()
    if (
        not value
        or value.startswith(('-', '/', '.'))
        or value.endswith(('/', '.'))
        or ".." in value
        or "@{" in value
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
    ):
        raise ArtifactError(f"{label} is not a safe Git ref")
    return value


def initialize_artifact_repository(workspace: str | Path, run_id: str) -> dict[str, object]:
    """Create a local checkpoint and review branch in an isolated task directory."""
    workspace_path = Path(workspace).expanduser()
    if workspace_path.is_symlink() or not workspace_path.is_dir():
        raise ArtifactError("artifact workspace must be a real directory")
    root = workspace_path.absolute()
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0:
        initialized = _git(root, "init", "-b", "main")
        if initialized.returncode != 0:
            _require_git(root, "init")
            _require_git(root, "checkout", "-b", "main")
    _require_git(root, "add", "-A")
    _require_git(
        root,
        "-c",
        "user.name=LightClaw",
        "-c",
        "user.email=local@lightclaw.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "LightClaw starting checkpoint",
    )
    base_commit = _require_git(root, "rev-parse", "HEAD")
    branch = _safe_branch(run_id)
    current = _require_git(root, "branch", "--show-current")
    if current != branch:
        exists = _git(root, "show-ref", "--verify", f"refs/heads/{branch}").returncode == 0
        if exists:
            _require_git(root, "switch", branch)
        else:
            _require_git(root, "switch", "-c", branch)
    return {
        "type": "git-checkpoint",
        "base_commit": base_commit,
        "branch": branch,
        "workspace": root.as_posix(),
        "published": False,
    }


def create_isolated_worktree(
    source_repository: str | Path,
    workspace: str | Path,
    run_id: str,
) -> dict[str, object]:
    """Create an optional real Git worktree without changing the source checkout."""
    source = Path(source_repository).expanduser().resolve()
    target = Path(workspace).expanduser().resolve()
    if _git(source, "rev-parse", "--is-inside-work-tree").returncode != 0:
        raise ArtifactError("source repository is not a Git worktree")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ArtifactError("isolated worktree target must be absent or empty")
    target.parent.mkdir(parents=True, exist_ok=True)
    branch = _safe_branch(run_id)
    result = _git(source, "worktree", "add", "-b", branch, target.as_posix(), "HEAD", timeout=120)
    if result.returncode != 0:
        raise ArtifactError(redact_text(result.stderr or result.stdout).strip()[-800:])
    return {
        "type": "git-worktree",
        "source": source.as_posix(),
        "workspace": target.as_posix(),
        "base_commit": _require_git(target, "rev-parse", "HEAD"),
        "branch": branch,
        "published": False,
    }


def create_patch_bundle(
    workspace: str | Path,
    output_dir: str | Path,
    *,
    run_id: str,
) -> dict[str, object]:
    """Stage an isolated workspace and write a private patch plus manifest."""
    root = Path(workspace).expanduser().absolute()
    output = Path(output_dir).expanduser().resolve()
    _require_git(root, "add", "-A")
    status = _git(root, "diff", "--cached", "--name-status", "-z", "--no-renames", "HEAD")
    if status.returncode != 0:
        detail = redact_text(status.stderr or status.stdout).strip()[-800:]
        raise ArtifactError(detail or "could not inspect changed paths")
    status_fields = status.stdout.split("\0")
    if status_fields and not status_fields[-1]:
        status_fields.pop()
    if len(status_fields) % 2:
        raise ArtifactError("Git returned malformed changed-path data")
    if len(status_fields) > 1000:
        raise ArtifactError("review artifacts support at most 500 changed paths; reduce the run scope")
    diff_stat = _require_git(root, "diff", "--cached", "--stat", "HEAD")
    branch = _require_git(root, "branch", "--show-current")
    base_commit = _require_git(root, "rev-parse", "HEAD")
    changed_paths = [
        {"status": change[:1] or "?", "path": path}
        for change, path in zip(status_fields[::2], status_fields[1::2], strict=True)
    ]
    patch_path = output / "changes.patch"
    manifest_path = output / "artifact.json"
    if patch_path.is_symlink():
        raise ArtifactError("refusing to replace a symlink patch path")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    patch_fd, raw_patch_temp = tempfile.mkstemp(prefix=".changes.", dir=output)
    os.close(patch_fd)
    patch_temp = Path(raw_patch_temp)
    try:
        patch_result = _git(
            root,
            "diff",
            "--cached",
            "--binary",
            "--no-ext-diff",
            f"--output={patch_temp}",
            "HEAD",
            timeout=120,
        )
        if patch_result.returncode != 0:
            detail = redact_text(patch_result.stderr or patch_result.stdout).strip()[-800:]
            raise ArtifactError(detail or "could not write the review patch")
        patch_sha256 = sha256_file(patch_temp)
        os.replace(patch_temp, patch_path)
    finally:
        patch_temp.unlink(missing_ok=True)
    manifest: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "workspace": root.as_posix(),
        "branch": branch,
        "base_commit": base_commit,
        "changed_paths": changed_paths,
        "diff_stat": diff_stat,
        "patch": patch_path.as_posix(),
        "patch_sha256": patch_sha256,
        "published": False,
    }
    _write_private(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    manifest["manifest"] = manifest_path.as_posix()
    return manifest


def accept_artifact(workspace: str | Path, run_id: str) -> dict[str, object]:
    """Commit the staged result locally; never push."""
    root = Path(workspace).expanduser().absolute()
    _require_git(root, "add", "-A")
    staged = _git(root, "diff", "--cached", "--quiet", "HEAD")
    if staged.returncode not in {0, 1}:
        raise ArtifactError("could not inspect staged artifact")
    if staged.returncode == 1:
        _require_git(
            root,
            "-c",
            "user.name=LightClaw",
            "-c",
            "user.email=local@lightclaw.invalid",
            "commit",
            "-m",
            f"LightClaw accepted result {run_id}",
        )
    return {
        "run_id": run_id,
        "workspace": root.as_posix(),
        "branch": _require_git(root, "branch", "--show-current"),
        "commit": _require_git(root, "rev-parse", "HEAD"),
        "published": False,
    }


def reject_artifact(workspace: str | Path, run_id: str) -> dict[str, object]:
    """Unstage a rejected result while preserving every workspace file for review."""
    root = Path(workspace).expanduser().absolute()
    _require_git(root, "reset", "--mixed", "HEAD")
    return {
        "run_id": run_id,
        "workspace": root.as_posix(),
        "preserved": True,
        "published": False,
    }


def _safe_selected_path(raw: str) -> str:
    candidate = PurePosixPath(str(raw).replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() in {"", "."}:
        raise ArtifactError(f"selected path must be safe and relative: {raw}")
    return candidate.as_posix()


def _validate_workspace_path(root: Path, relative: str, label: str) -> None:
    current = root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current /= part
        if current.is_symlink() or (
            index < len(parts) - 1 and current.exists() and not current.is_dir()
        ):
            raise ArtifactError(f"{label} contains a symlink or non-directory parent: {relative}")


def _open_workspace_directory(
    root: Path,
    parts: tuple[str, ...],
    *,
    label: str,
    create: bool = False,
    private: bool = False,
) -> int:
    try:
        return open_directory_at(root, parts, create=create, private=private)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ArtifactError(f"{label} contains a symlink or non-directory parent") from exc
        raise ArtifactError(f"{label} could not be opened safely") from exc


def _open_workspace_file(root: Path, relative: str, label: str) -> tuple[int, os.stat_result]:
    try:
        return open_regular_file_at(root, relative)
    except OSError as exc:
        if exc.errno == errno.EINVAL:
            raise ArtifactError(f"{label} is not a regular file: {relative}") from exc
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ArtifactError(f"{label} contains a symlink or non-regular file: {relative}") from exc
        raise ArtifactError(f"{label} is no longer available: {relative}") from exc


def _hash_fd(file_fd: int) -> str:
    digest = hashlib.sha256()
    while chunk := os.read(file_fd, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _copy_fd(source_fd: int, destination_fd: int) -> str:
    digest = hashlib.sha256()
    while chunk := os.read(source_fd, 1024 * 1024):
        digest.update(chunk)
        remaining = memoryview(chunk)
        while remaining:
            written = os.write(destination_fd, remaining)
            if not written:
                raise OSError("short write while copying selected artifact")
            remaining = remaining[written:]
    return digest.hexdigest()


def _copy_to_temp(
    source_fd: int,
    directory_fd: int,
    name: str,
    source_stat: os.stat_result,
) -> tuple[str, str]:
    temp_name = f".{name}.{secrets.token_hex(8)}.tmp"
    temp_fd = os.open(
        temp_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        digest = _copy_fd(source_fd, temp_fd)
        os.fchmod(temp_fd, stat.S_IMODE(source_stat.st_mode))
        os.utime(temp_fd, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
        os.fsync(temp_fd)
    except BaseException:
        os.close(temp_fd)
        _unlink_at(directory_fd, temp_name)
        raise
    os.close(temp_fd)
    return temp_name, digest


def _unlink_at(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def apply_selected_files(
    source_workspace: str | Path,
    target_workspace: str | Path,
    selected_paths: list[str],
    *,
    run_id: str,
    apply: bool = False,
    confirm_plan: str | None = None,
) -> dict[str, object]:
    """Preview or apply selected files with per-file atomic replacement and backups."""
    source_path = Path(source_workspace).expanduser()
    target_path = Path(target_workspace).expanduser()
    if (
        source_path.is_symlink()
        or target_path.is_symlink()
        or not source_path.is_dir()
        or not target_path.is_dir()
    ):
        raise ArtifactError("source and target must be real directories")
    source = source_path.resolve()
    target = target_path.resolve()
    paths = list(dict.fromkeys(_safe_selected_path(path) for path in selected_paths))
    if not paths:
        raise ArtifactError("at least one selected file is required")
    backup_id = _safe_selected_path(run_id)
    if "/" in backup_id:
        raise ArtifactError("run id must be a single safe path component")
    operations: list[dict[str, object]] = []
    planned: list[tuple[str, Path, Path | None, str, int, str | None, int | None]] = []
    for relative in paths:
        if PurePosixPath(relative).parts[0] == ".lightclaw-backups":
            raise ArtifactError("selected path uses the reserved backup directory")
        destination = target / relative
        _validate_workspace_path(source, relative, "selected source")
        source_fd, source_stat = _open_workspace_file(source, relative, "selected source")
        source_mode = stat.S_IMODE(source_stat.st_mode)
        try:
            source_sha256 = _hash_fd(source_fd)
        finally:
            os.close(source_fd)
        _validate_workspace_path(target, relative, "target path")
        destination_exists = destination.exists()
        if destination_exists and not destination.is_file():
            raise ArtifactError(f"selected target is not a regular file: {relative}")
        backup: Path | None = None
        target_sha256: str | None = None
        target_mode: int | None = None
        if destination_exists:
            target_fd, target_stat = _open_workspace_file(target, relative, "target path")
            try:
                target_sha256 = _hash_fd(target_fd)
                target_mode = stat.S_IMODE(target_stat.st_mode)
            finally:
                os.close(target_fd)
            backup_relative = f".lightclaw-backups/{backup_id}/{relative}"
            _validate_workspace_path(target, backup_relative, "backup path")
            backup = target / backup_relative
            if backup.exists():
                raise ArtifactError(f"backup already exists; refusing to overwrite it: {relative}")
        operation = {
            "path": relative,
            "change": "overwrite" if destination_exists else "create",
            "source_sha256": source_sha256,
            "source_mode": f"{source_mode:04o}",
            "target_sha256": target_sha256,
            "target_mode": f"{target_mode:04o}" if target_mode is not None else None,
            "backup": backup.as_posix() if backup else None,
        }
        operations.append(operation)
        planned.append(
            (relative, destination, backup, source_sha256, source_mode, target_sha256, target_mode)
        )

    plan_sha256 = hashlib.sha256(
        json.dumps(
            {
                "run_id": run_id,
                "source": source.as_posix(),
                "target": target.as_posix(),
                "operations": operations,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if apply and confirm_plan != plan_sha256:
        raise ArtifactError(
            "plan changed or confirmation is missing; preview again and confirm plan_sha256"
        )

    if apply:
        with tempfile.TemporaryDirectory(prefix="lightclaw-apply-") as staging_dir:
            staging_root = Path(staging_dir)
            staging_fd = os.open(
                staging_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            staged: list[tuple[str, str, str, int, str | None]] = []
            try:
                for index, (
                    relative,
                    _destination,
                    _backup,
                    expected_sha256,
                    expected_mode,
                    target_sha256,
                    _target_mode,
                ) in enumerate(planned):
                    source_fd, source_stat = _open_workspace_file(
                        source, relative, "selected source"
                    )
                    temp_name: str | None = None
                    stage_name = f"{index}.stage"
                    try:
                        if stat.S_IMODE(source_stat.st_mode) != expected_mode:
                            raise ArtifactError(
                                f"selected source permissions changed during apply: {relative}"
                            )
                        temp_name, copied_sha256 = _copy_to_temp(
                            source_fd, staging_fd, stage_name, source_stat
                        )
                        if copied_sha256 != expected_sha256:
                            raise ArtifactError(f"selected source changed during apply: {relative}")
                        os.rename(
                            temp_name,
                            stage_name,
                            src_dir_fd=staging_fd,
                            dst_dir_fd=staging_fd,
                        )
                        temp_name = None
                        staged.append(
                            (relative, stage_name, expected_sha256, expected_mode, target_sha256)
                        )
                    finally:
                        os.close(source_fd)
                        if temp_name is not None:
                            _unlink_at(staging_fd, temp_name)

                created_backups: list[tuple[tuple[str, ...], str, int, int]] = []
                backups_ready = False
                try:
                    for (
                        relative,
                        _destination,
                        backup,
                        _source_sha256,
                        _source_mode,
                        target_sha256,
                        target_mode,
                    ) in planned:
                        if backup is None:
                            continue
                        backup_parts = PurePosixPath(backup.relative_to(target).as_posix()).parts
                        backup_parent_fd = _open_workspace_directory(
                            target,
                            backup_parts[:-1],
                            label="backup path",
                            create=True,
                            private=True,
                        )
                        target_fd: int | None = None
                        temp_name = None
                        try:
                            target_fd, target_stat = _open_workspace_file(
                                target, relative, "target path"
                            )
                            if stat.S_IMODE(target_stat.st_mode) != target_mode:
                                raise ArtifactError(
                                    f"selected target permissions changed during apply: {relative}"
                                )
                            temp_name, backup_sha256 = _copy_to_temp(
                                target_fd, backup_parent_fd, backup_parts[-1], target_stat
                            )
                            if backup_sha256 != target_sha256:
                                raise ArtifactError(
                                    f"selected target changed during apply: {relative}"
                                )
                            backup_stat = os.stat(
                                temp_name, dir_fd=backup_parent_fd, follow_symlinks=False
                            )
                            try:
                                os.link(
                                    temp_name,
                                    backup_parts[-1],
                                    src_dir_fd=backup_parent_fd,
                                    dst_dir_fd=backup_parent_fd,
                                    follow_symlinks=False,
                                )
                            except FileExistsError as exc:
                                raise ArtifactError(
                                    f"backup already exists; refusing to overwrite it: {relative}"
                                ) from exc
                            created_backups.append(
                                (
                                    backup_parts[:-1],
                                    backup_parts[-1],
                                    backup_stat.st_dev,
                                    backup_stat.st_ino,
                                )
                            )
                        finally:
                            if target_fd is not None:
                                os.close(target_fd)
                            if temp_name is not None:
                                _unlink_at(backup_parent_fd, temp_name)
                            os.close(backup_parent_fd)
                    backups_ready = True
                finally:
                    if not backups_ready:
                        cleanup_failures: list[str] = []
                        for parent_parts, name, device, inode in created_backups:
                            parent_fd: int | None = None
                            try:
                                parent_fd = _open_workspace_directory(
                                    target, parent_parts, label="backup cleanup"
                                )
                                current_stat = os.stat(
                                    name, dir_fd=parent_fd, follow_symlinks=False
                                )
                                if (current_stat.st_dev, current_stat.st_ino) == (device, inode):
                                    os.unlink(name, dir_fd=parent_fd)
                                else:
                                    cleanup_failures.append(f"{name}: backup changed during cleanup")
                            except FileNotFoundError:
                                pass
                            except (ArtifactError, OSError) as exc:
                                cleanup_failures.append(f"{name}: {exc}")
                            finally:
                                if parent_fd is not None:
                                    os.close(parent_fd)
                        if cleanup_failures:
                            raise ArtifactError(
                                "backup preparation failed and cleanup was incomplete: "
                                + "; ".join(cleanup_failures)
                            )

                for relative, stage_name, expected_sha256, expected_mode, target_sha256 in staged:
                    source_fd, source_stat = _open_workspace_file(
                        staging_root, stage_name, "staged selected source"
                    )
                    parent_fd: int | None = None
                    temp_name = None
                    try:
                        parts = PurePosixPath(relative).parts
                        parent_fd = _open_workspace_directory(
                            target, parts[:-1], label="target path", create=True
                        )
                        if stat.S_IMODE(source_stat.st_mode) != expected_mode:
                            raise ArtifactError(
                                f"staged source permissions changed during apply: {relative}"
                            )
                        temp_name, copied_sha256 = _copy_to_temp(
                            source_fd, parent_fd, parts[-1], source_stat
                        )
                        if copied_sha256 != expected_sha256:
                            raise ArtifactError(f"staged source changed during apply: {relative}")
                        if target_sha256 is None:
                            try:
                                os.link(
                                    temp_name,
                                    parts[-1],
                                    src_dir_fd=parent_fd,
                                    dst_dir_fd=parent_fd,
                                    follow_symlinks=False,
                                )
                            except FileExistsError as exc:
                                raise ArtifactError(
                                    f"selected target appeared during apply: {relative}"
                                ) from exc
                            _unlink_at(parent_fd, temp_name)
                        else:
                            os.rename(
                                temp_name,
                                parts[-1],
                                src_dir_fd=parent_fd,
                                dst_dir_fd=parent_fd,
                            )
                        temp_name = None
                    finally:
                        os.close(source_fd)
                        if parent_fd is not None:
                            if temp_name is not None:
                                _unlink_at(parent_fd, temp_name)
                            os.close(parent_fd)
            finally:
                os.close(staging_fd)
    return {
        "run_id": run_id,
        "source": source.as_posix(),
        "target": target.as_posix(),
        "applied": bool(apply),
        "plan_sha256": plan_sha256,
        "operations": operations,
        "unrelated_paths_preserved": True,
    }


def build_pull_request_preview(
    workspace: str | Path,
    receipt_path: str | Path,
    *,
    run_id: str,
    title: str,
    base: str = "main",
) -> dict[str, object]:
    """Build a complete PR preview without network writes."""
    root = Path(workspace).expanduser().absolute()
    receipt_file = Path(receipt_path).expanduser()
    try:
        receipt = read_receipt(receipt_file)
    except ValueError as exc:
        raise ArtifactError("private receipt is missing or invalid") from exc
    if str(receipt.get("run_id") or "") != run_id:
        raise ArtifactError("private receipt belongs to a different run")
    branch = _safe_git_ref(_require_git(root, "branch", "--show-current"), "branch")
    base = _safe_git_ref(base, "base branch")
    remote = _require_git(root, "remote", "get-url", "origin")
    status = _require_git(root, "status", "--porcelain=v1", "--untracked-files=all")
    local_base = base if _git(root, "show-ref", "--verify", f"refs/heads/{base}").returncode == 0 else ""
    remote_base = (
        f"origin/{base}"
        if _git(root, "show-ref", "--verify", f"refs/remotes/origin/{base}").returncode == 0
        else ""
    )
    comparison_base = local_base or remote_base
    commits_ahead: int | None = None
    if comparison_base:
        raw_count = _require_git(root, "rev-list", "--count", f"{comparison_base}..{branch}")
        commits_ahead = int(raw_count)
    ready_to_publish = not status and (commits_ahead is None or commits_ahead > 0)
    checks = receipt.get("checks") if isinstance(receipt.get("checks"), list) else []
    evidence_lines = []
    for check in checks[:20]:
        if isinstance(check, dict):
            marker = "PASS" if check.get("passed") else "FAIL"
            evidence_lines.append(f"- [{marker}] {check.get('name')}: {check.get('evidence')}")
    body = "\n".join(
        [
            "## LightClaw run",
            "",
            f"Run: `{run_id}`",
            f"Goal: {receipt.get('original_goal', '')}",
            f"Scope: {receipt.get('approved_scope', '')}",
            f"Risk/capability: `{receipt.get('risk_level', 'unknown')}` / `{receipt.get('capability_profile', 'unknown')}`",
            f"Diff: {receipt.get('diff_summary', 'not available')}",
            "",
            "## Test evidence",
            "",
            *(evidence_lines or ["- No test evidence recorded."]),
            "",
            "Generated from a private local receipt; secrets and private recovery context are omitted.",
        ]
    )
    return {
        "run_id": run_id,
        "workspace": root.as_posix(),
        "remote": redact_text(remote),
        "branch": branch,
        "base": base,
        "title": title,
        "body": redact_text(body),
        "commands": [
            ["git", "push", "--set-upstream", "origin", branch],
            ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title],
        ],
        "working_tree_clean": not bool(status),
        "commits_ahead": commits_ahead,
        "ready_to_publish": ready_to_publish,
        "required_local_action": (
            "accept the artifact into a local commit first" if not ready_to_publish else None
        ),
        "requires_confirmation": run_id,
        "published": False,
    }


def publish_pull_request(
    preview: dict[str, object],
    *,
    confirmation: str,
) -> dict[str, object]:
    """Push and create a PR only when the exact run id is confirmed."""
    run_id = str(preview.get("run_id") or "")
    if not run_id or confirmation != run_id:
        raise ArtifactError("PR publication requires the exact run id confirmation")
    if preview.get("ready_to_publish") is not True:
        raise ArtifactError("PR publication requires a clean, accepted local artifact commit")
    root = Path(str(preview.get("workspace") or "")).absolute()
    branch = str(preview.get("branch") or "")
    base = str(preview.get("base") or "main")
    title = str(preview.get("title") or "LightClaw result")
    auth = subprocess.run(
        ["gh", "auth", "status"],
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
        env=delegated_process_env(),
    )
    if auth.returncode != 0:
        raise ArtifactError("gh CLI is not authenticated")
    _require_git(root, "push", "--set-upstream", "origin", branch, timeout=180)
    with tempfile.TemporaryDirectory(prefix="lightclaw-pr-") as temporary:
        body_file = Path(temporary) / "body.md"
        _write_private(body_file, str(preview.get("body") or ""))
        try:
            with directory_command_at(
                root, (),
                "gh",
                "pr",
                "create",
                "--base",
                base,
                "--head",
                branch,
                "--title",
                title,
                "--body-file",
                body_file.as_posix(),
            ) as (command, pass_fds):
                created = subprocess.run(
                    command,
                    pass_fds=pass_fds,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=120,
                    env=delegated_process_env(),
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ArtifactError("GitHub pull request creation could not run") from exc
    if created.returncode != 0:
        raise ArtifactError(redact_text(created.stderr or created.stdout).strip()[-800:])
    result = dict(preview)
    result["published"] = True
    result["url"] = created.stdout.strip().splitlines()[-1]
    return result
