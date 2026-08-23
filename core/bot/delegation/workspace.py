"""Workspace pathing and file-change snapshot helpers."""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from ...workspaces import register_task_workspace, validate_workspace_root


class DelegationWorkspaceMixin:
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
        candidate = root / base_name
        idx = 2
        while candidate.exists():
            candidate = root / f"{base_name}_{idx}"
            idx += 1
        candidate.mkdir(parents=True, exist_ok=False)
        register_task_workspace(root, candidate, goal_text)
        return candidate

    def _workspace_rel_label(self, workspace: Path) -> str:
        root = validate_workspace_root(self.config.workspace_path)
        try:
            return workspace.resolve().relative_to(root).as_posix()
        except Exception:
            return workspace.resolve().as_posix()

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
                        digest = hashlib.sha256(current.read_bytes()).hexdigest()
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
