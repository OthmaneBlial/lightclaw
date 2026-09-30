"""Creation, checkpointing, and conservative rollback for task workspaces."""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

from .fs import atomic_write_text_at, open_directory_at, read_text_bounded_at
from .security import delegated_process_env

METADATA_DIRNAME = ".lightclaw-meta"
TASK_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,159}$")


class WorkspaceSafetyError(ValueError):
    """Raised when a workspace cannot be proven to be LightClaw-owned."""


def _atomic_private_json(
    root_fd: int, relative: str, payload: dict[str, object], expected_content: str | None
) -> None:
    try:
        atomic_write_text_at(
            root_fd,
            relative,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            expected_content=expected_content,
            private_parents=True,
            mode=0o600,
        )
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise WorkspaceSafetyError("task metadata directory must not be a symlink") from exc
        raise


def validate_workspace_root(raw_root: str | Path) -> Path:
    """Resolve a workspace root while rejecting a symlink as the configured root."""
    requested = Path(raw_root).expanduser()
    if requested.is_symlink():
        raise WorkspaceSafetyError("configured workspace root must not be a symlink")
    root = requested.resolve()
    if root == Path(root.anchor):
        raise WorkspaceSafetyError("filesystem root cannot be used as a task workspace")
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise WorkspaceSafetyError("configured workspace root is not a directory")
    return root


def ensure_private_workspace_dir(root: str | Path, *parts: str) -> Path:
    """Create a private workspace directory without following symlinks."""
    for part in parts:
        if part in {"", ".", ".."} or "/" in part or "\\" in part:
            raise WorkspaceSafetyError("private workspace path contains an unsafe directory name")
    current = validate_workspace_root(root)
    try:
        directory_fd = open_directory_at(current, tuple(parts), create=True, private=True)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise WorkspaceSafetyError(
                "private workspace path must contain real directories and must not be a symlink"
            ) from exc
        raise
    os.close(directory_fd)
    return current.joinpath(*parts)


def capture_git_checkpoint(workspace: Path) -> dict[str, object]:
    """Capture starting Git identity without mutating the workspace."""

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", workspace.as_posix(), *args],
            text=True,
            capture_output=True,
            timeout=5,
            check=False, env=delegated_process_env(),
        )

    try:
        inside = run("rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.TimeoutExpired):
        return {"is_git": False, "commit": None, "dirty": False, "status": []}
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return {"is_git": False, "commit": None, "dirty": False, "status": []}

    commit_result = run("rev-parse", "HEAD")
    status_result = run("status", "--porcelain=v1", "--untracked-files=all")
    status = [line[:500] for line in status_result.stdout.splitlines()[:200]]
    return {
        "is_git": True,
        "commit": commit_result.stdout.strip() if commit_result.returncode == 0 else None,
        "dirty": bool(status),
        "status": status,
    }


def register_task_workspace(root: Path, workspace: Path, goal: str) -> dict[str, object]:
    """Record proof that a freshly created task directory belongs to LightClaw."""
    root = validate_workspace_root(root)
    workspace = workspace.resolve()
    try:
        relative = workspace.relative_to(root)
    except ValueError as exc:
        raise WorkspaceSafetyError("task workspace escapes configured root") from exc
    if len(relative.parts) != 1 or not TASK_NAME_PATTERN.fullmatch(relative.name):
        raise WorkspaceSafetyError("task workspace must be one direct, safe child of the root")
    if workspace.is_symlink() or not workspace.is_dir():
        raise WorkspaceSafetyError("task workspace must be a real directory")

    metadata: dict[str, object] = {
        "schema_version": 1,
        "owner": "lightclaw",
        "task_name": relative.name,
        "workspace": workspace.as_posix(),
        "workspace_root": root.as_posix(),
        "goal_preview": re.sub(r"\s+", " ", goal).strip()[:240],
        "created_at": int(time.time()),
        "state": "active",
        "starting_files": [],
        "starting_git": capture_git_checkpoint(workspace),
    }
    root_fd = open_directory_at(root, ())
    try:
        _atomic_private_json(
            root_fd, f"{METADATA_DIRNAME}/{relative.name}.json", metadata, None
        )
    finally:
        os.close(root_fd)
    return metadata


def _resolve_owned_task_at(
    root_path: Path, root_fd: int, task_name: str
) -> tuple[Path, Path, dict[str, object], str, bool]:
    name = str(task_name or "").strip()
    if not TASK_NAME_PATTERN.fullmatch(name):
        raise WorkspaceSafetyError("task name must be a single safe workspace label")

    relative_metadata = f"{METADATA_DIRNAME}/{name}.json"
    try:
        raw_metadata = read_text_bounded_at(root_fd, relative_metadata, 1024 * 1024)
    except FileNotFoundError as exc:
        raise WorkspaceSafetyError("no LightClaw ownership record exists for this task") from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise WorkspaceSafetyError(
                "task ownership record directory must not be a symlink"
            ) from exc
        raise WorkspaceSafetyError("task ownership record is unreadable") from exc
    try:
        metadata = json.loads(raw_metadata)
    except ValueError as exc:
        raise WorkspaceSafetyError("task ownership record is unreadable") from exc
    if not isinstance(metadata, dict) or metadata.get("owner") != "lightclaw":
        raise WorkspaceSafetyError("task ownership record is invalid")
    if metadata.get("task_name") != name or metadata.get("workspace_root") != root_path.as_posix():
        raise WorkspaceSafetyError("task ownership record does not match this workspace root")

    workspace = root_path / name
    try:
        workspace_stat = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        workspace_stat = None
    if workspace_stat is not None and stat.S_ISLNK(workspace_stat.st_mode):
        raise WorkspaceSafetyError("refusing to undo a symlinked task workspace")
    return workspace, root_path / relative_metadata, metadata, raw_metadata, bool(
        workspace_stat and stat.S_ISDIR(workspace_stat.st_mode)
    )


def resolve_owned_task(root: str | Path, task_name: str) -> tuple[Path, Path, dict[str, object]]:
    """Resolve a task only when external metadata proves LightClaw ownership."""
    root_path = validate_workspace_root(root)
    root_fd = open_directory_at(root_path, ())
    try:
        workspace, metadata_path, metadata, _raw, _exists = _resolve_owned_task_at(
            root_path, root_fd, task_name
        )
        return workspace, metadata_path, metadata
    finally:
        os.close(root_fd)


def undo_owned_task(root: str | Path, task_name: str, *, apply: bool = False) -> dict[str, object]:
    """Preview or delete one LightClaw-created task directory and nothing else."""
    root_path = validate_workspace_root(root)
    root_fd = open_directory_at(root_path, ())
    try:
        workspace, metadata_path, metadata, raw_metadata, exists = _resolve_owned_task_at(
            root_path, root_fd, task_name
        )
        result: dict[str, object] = {
            "task_name": task_name,
            "workspace": workspace.as_posix(),
            "exists": exists,
            "applied": False,
        }
        if not apply or not exists:
            return result
        if not shutil.rmtree.avoids_symlink_attacks:
            raise WorkspaceSafetyError("safe task workspace deletion is unavailable")

        shutil.rmtree(workspace.name, dir_fd=root_fd)
        metadata["state"] = "undone"
        metadata["undone_at"] = int(time.time())
        _atomic_private_json(
            root_fd,
            metadata_path.relative_to(root_path).as_posix(),
            metadata,
            raw_metadata,
        )
        result["applied"] = True
        return result
    finally:
        os.close(root_fd)
