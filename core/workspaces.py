"""Creation, checkpointing, and conservative rollback for task workspaces."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

METADATA_DIRNAME = ".lightclaw-meta"
TASK_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,159}$")


class WorkspaceSafetyError(ValueError):
    """Raised when a workspace cannot be proven to be LightClaw-owned."""


def _atomic_private_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw_temp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        path.chmod(0o600)
    finally:
        temp.unlink(missing_ok=True)


def validate_workspace_root(raw_root: str | Path) -> Path:
    """Resolve a workspace root while rejecting a symlink as the configured root."""
    requested = Path(raw_root).expanduser()
    if requested.exists() and requested.is_symlink():
        raise WorkspaceSafetyError("configured workspace root must not be a symlink")
    root = requested.resolve()
    if root == Path(root.anchor):
        raise WorkspaceSafetyError("filesystem root cannot be used as a task workspace")
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise WorkspaceSafetyError("configured workspace root is not a directory")
    return root


def capture_git_checkpoint(workspace: Path) -> dict[str, object]:
    """Capture starting Git identity without mutating the workspace."""

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", workspace.as_posix(), *args],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
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
    _atomic_private_json(root / METADATA_DIRNAME / f"{relative.name}.json", metadata)
    return metadata


def resolve_owned_task(root: str | Path, task_name: str) -> tuple[Path, Path, dict[str, object]]:
    """Resolve a task only when external metadata proves LightClaw ownership."""
    root_path = validate_workspace_root(root)
    name = str(task_name or "").strip()
    if not TASK_NAME_PATTERN.fullmatch(name):
        raise WorkspaceSafetyError("task name must be a single safe workspace label")

    metadata_path = root_path / METADATA_DIRNAME / f"{name}.json"
    if metadata_path.is_symlink() or not metadata_path.is_file():
        raise WorkspaceSafetyError("no LightClaw ownership record exists for this task")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceSafetyError("task ownership record is unreadable") from exc
    if not isinstance(metadata, dict) or metadata.get("owner") != "lightclaw":
        raise WorkspaceSafetyError("task ownership record is invalid")
    if metadata.get("task_name") != name or metadata.get("workspace_root") != root_path.as_posix():
        raise WorkspaceSafetyError("task ownership record does not match this workspace root")

    workspace = root_path / name
    if workspace.is_symlink():
        raise WorkspaceSafetyError("refusing to undo a symlinked task workspace")
    if workspace.resolve(strict=False).parent != root_path:
        raise WorkspaceSafetyError("task workspace is not a direct child of the configured root")
    return workspace, metadata_path, metadata


def undo_owned_task(root: str | Path, task_name: str, *, apply: bool = False) -> dict[str, object]:
    """Preview or delete one LightClaw-created task directory and nothing else."""
    workspace, metadata_path, metadata = resolve_owned_task(root, task_name)
    exists = workspace.is_dir()
    result: dict[str, object] = {
        "task_name": task_name,
        "workspace": workspace.as_posix(),
        "exists": exists,
        "applied": False,
    }
    if not apply or not exists:
        return result

    shutil.rmtree(workspace)
    metadata["state"] = "undone"
    metadata["undone_at"] = int(time.time())
    _atomic_private_json(metadata_path, metadata)
    result["applied"] = True
    return result
