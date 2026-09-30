"""Workspace pathing and file-change snapshot helpers."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from pathlib import Path

from ...artifacts import ArtifactError, initialize_artifact_repository
from ...fs import sha256_file
from ...jobs import JobStateError
from ...logging_setup import log
from ...workspaces import (
    capture_git_checkpoint,
    ensure_private_workspace_dir,
    register_task_workspace,
    undo_owned_task,
    validate_workspace_root,
)


async def await_thread_completion(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        except Exception:
            log.exception("Off-thread operation failed during cancellation")
        raise


class DelegationWorkspaceMixin:
    async def _resolve_task_workspace(
        self, goal_text: str, workspace_dir: Path | str | None
    ) -> tuple[Path, bool]:
        if workspace_dir is None:
            return await self._create_task_workspace_safely(goal_text), True
        target = Path(workspace_dir).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        return target, False

    async def _cleanup_unclaimed_task_workspace(
        self, workspace: Path, *, run_id: str | None = None, job_store=None
    ) -> None:
        if job_store is not None and run_id:
            try:
                await await_thread_completion(job_store.get_job, run_id)
            except JobStateError:
                pass
            except Exception:
                log.exception("Could not establish durable ownership of task workspace %s", workspace)
                return
            else:
                return
        try:
            await await_thread_completion(
                undo_owned_task,
                self.config.workspace_path,
                workspace.name,
                apply=True,
            )
        except Exception:
            log.exception("Could not remove unclaimed task workspace %s", workspace)

    async def _await_task_workspace_preflight(
        self,
        workspace: Path,
        function,
        *args,
        run_id: str | None = None,
        job_store=None,
        **kwargs,
    ):
        try:
            return await await_thread_completion(function, *args, **kwargs)
        except asyncio.CancelledError:
            await self._cleanup_unclaimed_task_workspace(
                workspace, run_id=run_id, job_store=job_store
            )
            raise
        except Exception as exc:
            if not isinstance(exc, ArtifactError):
                await self._cleanup_unclaimed_task_workspace(
                    workspace, run_id=run_id, job_store=job_store
                )
            raise

    async def _prepare_task_workspace_checkpoint(
        self,
        workspace: Path,
        run_id: str,
        *,
        initialize_artifact: bool,
        owns_workspace: bool,
    ):
        function = initialize_artifact_repository if initialize_artifact else capture_git_checkpoint
        args = (workspace, run_id) if initialize_artifact else (workspace,)
        if owns_workspace:
            return await self._await_task_workspace_preflight(workspace, function, *args)
        return await await_thread_completion(function, *args)

    async def _durable_job_heartbeat(self, store, run_id: str, *, worker_pid: int | None = None):
        try:
            return await await_thread_completion(
                store.heartbeat, run_id, worker_pid=worker_pid
            ), False
        except JobStateError:
            return None, True
        except Exception:
            log.exception("Durable job heartbeat failed; retrying run %s", run_id)
            return None, False

    async def _cancel_durable_delegation(
        self, store, session_id: str, run_id: str, workspace: Path, *, owns_workspace: bool
    ) -> None:
        async def cancel_once() -> None:
            try:
                job = await await_thread_completion(store.get_job, run_id)
            except JobStateError:
                if owns_workspace:
                    await self._cleanup_unclaimed_task_workspace(workspace)
                return
            status = str(job["status"])
            if status == "running":
                await await_thread_completion(store.request_cancel, run_id)
            for lane in job["lanes"]:
                if lane["status"] in {"queued", "running"}:
                    await await_thread_completion(
                        store.update_lane, run_id, str(lane["label"]), "canceled"
                    )
            if status in {"queued", "awaiting_approval", "paused", "stalled"}:
                await await_thread_completion(store.request_cancel, run_id)
            elif status in {"running", "cancel_requested"}:
                await await_thread_completion(store.mark_canceled, run_id)

        try:
            for attempt in range(2):
                try:
                    await cancel_once()
                    return
                except sqlite3.OperationalError:
                    if attempt == 0:
                        log.warning("Retrying durable job cancellation after SQLite error: %s", run_id)
                        continue
                    log.exception("Could not cancel durable delegation job %s", run_id)
                except Exception:
                    log.exception("Could not cancel durable delegation job %s", run_id)
                    return
        finally:
            if self._active_run_ids_by_session.get(session_id) == run_id:
                self._active_run_ids_by_session.pop(session_id, None)

    async def _handle_durable_setup_failure(
        self, error: Exception, store, session_id: str, run_id: str, workspace: Path, owns: bool
    ) -> str:
        await self._cancel_durable_delegation(
            store, session_id, run_id, workspace, owns_workspace=owns
        )
        if isinstance(error, JobStateError):
            return f"⚠️ Durable job control refused the run: {error}"
        log.error(
            "Durable job setup failed before local agent launch: %s",
            run_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        return (
            "⚠️ Durable job setup failed; the local agent was not started. "
            f"Run ID: `{run_id}`. See local logs for details."
        )

    async def _finalize_durable_delegation(self, store, run_id: str, result) -> str:
        succeeded = bool(result.get("ok"))
        lane_status = "succeeded" if succeeded else "failed"
        error = "" if succeeded else str(result.get("stderr") or "delegation failed")[:500]
        for attempt in range(2):
            try:
                await await_thread_completion(
                    store.update_lane, run_id, "delegation", lane_status, error=error
                )
                current = await await_thread_completion(store.get_job, run_id)
                if current["status"] in {"succeeded", "failed", "canceled"}:
                    if current["status"] == "canceled" and succeeded:
                        return "job was canceled before finalization"
                    if current["status"] not in {lane_status, "canceled"}:
                        return f"job ended as {current['status']} before finalization"
                    return ""
                if current["status"] == "cancel_requested":
                    await await_thread_completion(store.mark_canceled, run_id)
                else:
                    await await_thread_completion(
                        store.finish, run_id, succeeded=succeeded, error=error
                    )
                return ""
            except sqlite3.OperationalError:
                if attempt == 0:
                    log.warning("Retrying durable run finalization after SQLite error: %s", run_id)
                    continue
                log.exception("Could not finalize durable run %s", run_id)
                return "OperationalError"
            except Exception as exc:
                log.exception("Could not finalize durable run %s", run_id)
                return type(exc).__name__
    @staticmethod
    def _slugify_goal_name(text: str, max_len: int = 56) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
        if not slug:
            return "task"
        slug = slug[:max_len].strip("-")
        return slug or "task"

    def _create_task_workspace(self, goal_text: str) -> Path:
        root = validate_workspace_root(self.config.workspace_path)

        stamp = time.strftime("%Y%m%d_%H%M%S")
        slug = self._slugify_goal_name(goal_text)
        base_name = f"{stamp}_{slug}"
        idx = 1
        while True:
            name = base_name if idx == 1 else f"{base_name}_{idx}"
            candidate = root / name
            try:
                candidate.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                idx += 1
        try:
            register_task_workspace(root, candidate, goal_text)
        except Exception:
            try:
                candidate.rmdir()
            except OSError:
                pass
            raise
        return candidate

    async def _create_task_workspace_safely(self, goal_text: str) -> Path:
        creation = asyncio.create_task(asyncio.to_thread(self._create_task_workspace, goal_text))
        try:
            return await asyncio.shield(creation)
        except asyncio.CancelledError:
            try:
                workspace = await asyncio.shield(creation)
            except Exception:
                log.exception("Task workspace creation failed during cancellation")
            else:
                await self._cleanup_unclaimed_task_workspace(workspace)
            raise

    def _workspace_rel_label(self, workspace: Path) -> str:
        root = validate_workspace_root(self.config.workspace_path)
        try:
            return workspace.resolve().relative_to(root).as_posix()
        except Exception:
            return workspace.resolve().as_posix()

    def _receipt_output_dir(self, run_id: str) -> Path:
        return ensure_private_workspace_dir(
            self.config.workspace_path, ".lightclaw-meta", "receipts", run_id
        )

    def _build_delegation_prompt(self, task: str, workspace: Path | None = None) -> str:
        target_workspace = (workspace or Path(self.config.workspace_path).resolve()).resolve()
        workspace_path = target_workspace.as_posix()
        return (
            "You are a local coding agent delegated by LightClaw.\n"
            f"Workspace root: {workspace_path}\n\n"
            "Requirements:\n"
            "- Implement the task directly by creating/editing files in this workspace.\n"
            "- Do not ask for confirmation; make reasonable assumptions and proceed.\n"
            "- If the task is large, still perform as much as possible in one run.\n"
            "- Do not dump full source files in the final response.\n"
            "- End with a concise summary of what was created/updated.\n\n"
            "TASK:\n"
            f"{task}\n"
        )

    def _snapshot_workspace_state(
        self,
        workspace: Path | None = None,
    ) -> dict[str, tuple[int, int]]:
        """Snapshot workspace file metadata for before/after change detection."""
        workspace = (workspace or Path(self.config.workspace_path).resolve()).resolve()
        snapshot: dict[str, tuple[int, int]] = {}
        for path in workspace.rglob("*"):
            try:
                relative_parts = path.relative_to(workspace).parts
            except ValueError:
                continue
            if ".git" in relative_parts or ".lightclaw-meta" in relative_parts:
                continue
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except Exception:
                continue
            rel = path.relative_to(workspace).as_posix()
            snapshot[rel] = (int(stat.st_size), int(stat.st_mtime_ns))
        return snapshot

    @staticmethod
    def _summarize_workspace_delta(
        before: dict[str, tuple[int, int]],
        after: dict[str, tuple[int, int]],
        max_items_per_group: int = 12,
    ) -> str:
        before_paths = set(before.keys())
        after_paths = set(after.keys())

        created = sorted(after_paths - before_paths)
        deleted = sorted(before_paths - after_paths)
        updated = sorted(
            path for path in (before_paths & after_paths) if before[path] != after[path]
        )

        total = len(created) + len(updated) + len(deleted)
        if total == 0:
            return "No workspace file changes detected."

        lines = [
            "✅ Workspace changes detected:",
            f"- Created: {len(created)}",
            f"- Updated: {len(updated)}",
            f"- Deleted: {len(deleted)}",
        ]

        for label, items in (("Created", created), ("Updated", updated), ("Deleted", deleted)):
            if not items:
                continue
            for path in items[:max_items_per_group]:
                lines.append(f"- {label}: `{path}`")
            remaining = len(items) - max_items_per_group
            if remaining > 0:
                lines.append(f"- {label}: ... and {remaining} more")

        return "\n".join(lines)

    @staticmethod
    def _workspace_file_changes(
        workspace: Path,
        before: dict[str, tuple[int, int]],
        after: dict[str, tuple[int, int]],
    ) -> list[dict[str, object]]:
        """Return bounded, content-addressed file evidence for a receipt."""
        before_paths = set(before)
        after_paths = set(after)
        changes: list[dict[str, object]] = []
        groups = (
            ("created", sorted(after_paths - before_paths)),
            (
                "updated",
                sorted(path for path in before_paths & after_paths if before[path] != after[path]),
            ),
            ("deleted", sorted(before_paths - after_paths)),
        )
        for change, paths in groups:
            for relative in paths[:500]:
                current = workspace / relative
                size = after.get(relative, before.get(relative, (0, 0)))[0]
                digest = ""
                if change != "deleted" and current.is_file() and not current.is_symlink():
                    try:
                        digest = sha256_file(current)
                    except OSError:
                        digest = "unavailable"
                changes.append(
                    {
                        "path": relative,
                        "change": change,
                        "bytes": int(size),
                        "sha256": digest,
                    }
                )
        return changes

    @staticmethod
    def _compact_diff_summary(file_changes: list[dict[str, object]]) -> str:
        counts = {"created": 0, "updated": 0, "deleted": 0}
        for item in file_changes:
            change = str(item.get("change") or "")
            if change in counts:
                counts[change] += 1
        return (
            f"{counts['created']} created, {counts['updated']} updated, "
            f"{counts['deleted']} deleted"
        )
