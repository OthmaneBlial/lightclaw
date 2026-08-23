"""Reviewable Git artifacts, selective local apply, and explicit PR publishing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from .receipts import _write_private
from .security import delegated_process_env, redact_text


class ArtifactError(ValueError):
    """Raised when an artifact operation cannot be proven safe."""


def _git(workspace: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", workspace.as_posix(), *args],
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
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise ArtifactError("artifact workspace must be a real directory")
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
    root = Path(workspace).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    _require_git(root, "add", "-A")
    status_text = _require_git(root, "status", "--porcelain=v1", "--untracked-files=all")
    patch = _require_git(root, "diff", "--cached", "--binary", "--no-ext-diff", "HEAD", timeout=120)
    diff_stat = _require_git(root, "diff", "--cached", "--stat", "HEAD")
    branch = _require_git(root, "branch", "--show-current")
    base_commit = _require_git(root, "rev-parse", "HEAD")
    changed_paths: list[dict[str, str]] = []
    for line in status_text.splitlines()[:500]:
        if len(line) < 4:
            continue
        changed_paths.append({"status": line[:2].strip() or "?", "path": line[3:]})
    patch_path = output / "changes.patch"
    manifest_path = output / "artifact.json"
    _write_private(patch_path, patch + ("\n" if patch and not patch.endswith("\n") else ""))
    manifest: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "workspace": root.as_posix(),
        "branch": branch,
        "base_commit": base_commit,
        "changed_paths": changed_paths,
        "diff_stat": diff_stat,
        "patch": patch_path.as_posix(),
        "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        "published": False,
    }
    _write_private(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    manifest["manifest"] = manifest_path.as_posix()
    return manifest


def accept_artifact(workspace: str | Path, run_id: str) -> dict[str, object]:
    """Commit the staged result locally; never push."""
    root = Path(workspace).expanduser().resolve()
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
    root = Path(workspace).expanduser().resolve()
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


def apply_selected_files(
    source_workspace: str | Path,
    target_workspace: str | Path,
    selected_paths: list[str],
    *,
    run_id: str,
    apply: bool = False,
) -> dict[str, object]:
    """Preview or atomically copy selected files, backing up only overwritten targets."""
    source = Path(source_workspace).expanduser().resolve()
    target = Path(target_workspace).expanduser().resolve()
    if not source.is_dir() or not target.is_dir() or target.is_symlink():
        raise ArtifactError("source and target must be real directories")
    paths = list(dict.fromkeys(_safe_selected_path(path) for path in selected_paths))
    if not paths:
        raise ArtifactError("at least one selected file is required")
    operations: list[dict[str, object]] = []
    backup_root = target / ".lightclaw-backups" / _safe_selected_path(run_id)
    for relative in paths:
        source_file = source / relative
        destination = target / relative
        if source_file.is_symlink() or not source_file.is_file():
            raise ArtifactError(f"selected source is not a regular file: {relative}")
        try:
            source_file.resolve(strict=True).relative_to(source)
        except ValueError as exc:
            raise ArtifactError(f"selected source escapes workspace: {relative}") from exc
        if destination.is_symlink():
            raise ArtifactError(f"refusing symlink target: {relative}")
        try:
            destination.resolve(strict=False).relative_to(target)
        except ValueError as exc:
            raise ArtifactError(f"selected path escapes target: {relative}") from exc
        operation = {
            "path": relative,
            "change": "overwrite" if destination.exists() else "create",
            "source_sha256": hashlib.sha256(source_file.read_bytes()).hexdigest(),
            "backup": (backup_root / relative).as_posix() if destination.exists() else None,
        }
        operations.append(operation)
        if not apply:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            backup = backup_root / relative
            backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(destination, backup)
        fd, raw_temp = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        os.close(fd)
        temp = Path(raw_temp)
        try:
            shutil.copy2(source_file, temp)
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
    return {
        "run_id": run_id,
        "source": source.as_posix(),
        "target": target.as_posix(),
        "applied": bool(apply),
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
    root = Path(workspace).expanduser().resolve()
    receipt_file = Path(receipt_path).expanduser().resolve()
    try:
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError("private receipt is missing or invalid") from exc
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
    root = Path(str(preview.get("workspace") or "")).resolve()
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
    body_file = Path(_require_git(root, "rev-parse", "--git-path", "lightclaw-pr-body.md"))
    if not body_file.is_absolute():
        body_file = (root / body_file).resolve()
    _write_private(body_file, str(preview.get("body") or ""))
    try:
        created = subprocess.run(
            [
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
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
            env=delegated_process_env(),
        )
    finally:
        body_file.unlink(missing_ok=True)
    if created.returncode != 0:
        raise ArtifactError(redact_text(created.stderr or created.stdout).strip()[-800:])
    result = dict(preview)
    result["published"] = True
    result["url"] = created.stdout.strip().splitlines()[-1]
    return result
