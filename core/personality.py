"""Runtime path, personality loading, and system prompt construction."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from config import Config

from .constants import FALLBACK_IDENTITY, FILE_IO_RULES, PROJECT_ROOT


def runtime_root_from_workspace(workspace_path: str) -> Path:
    """Derive runtime root from workspace path."""
    workspace = Path(workspace_path).resolve()
    if workspace.name == "workspace":
        return workspace.parent
    return workspace


def personality_search_paths(workspace_path: str) -> list[Path]:
    """Return preferred locations for personality files (new path first, legacy fallback)."""
    workspace = Path(workspace_path).resolve()
    runtime_root = runtime_root_from_workspace(workspace_path)
    paths = [runtime_root]
    if workspace != runtime_root:
        paths.append(workspace)
    return paths


def resolve_runtime_path(path_value: str) -> Path:
    """Resolve configured paths relative to LIGHTCLAW_HOME or project root."""
    runtime_home = os.getenv("LIGHTCLAW_HOME", "").strip()
    base_dir = Path(runtime_home).expanduser().resolve() if runtime_home else PROJECT_ROOT
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def load_personality(workspace_path: str) -> str:
    """Load personality from runtime files (SOUL.md, IDENTITY.md, USER.md).

    Preferred path is runtime root (e.g. .lightclaw/). Falls back to legacy workspace path.
    """
    files = ["IDENTITY.md", "SOUL.md", "USER.md"]
    parts = []
    search_paths = personality_search_paths(workspace_path)

    for filename in files:
        for base in search_paths:
            filepath = base / filename
            if not filepath.exists():
                continue
            try:
                content = filepath.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(content)
                    break
            except Exception:
                continue

    if not parts:
        return FALLBACK_IDENTITY

    return "\n\n---\n\n".join(parts)


def build_system_prompt(
    config: Config,
    personality: str,
    memories_text: str,
    session_summary: str,
    skills_text: str = "",
) -> str:
    """Build the full system prompt with identity, memories, and summary."""
    parts = [
        personality,
        f"## Current Time\n{datetime.now().strftime('%Y-%m-%d %H:%M (%A)')}",
        f"## Provider\n{config.llm_provider} ({config.llm_model})",
        (
            "## Delegation Guardrails\n"
            "- Never claim or simulate local-agent execution unless LightClaw has already done it.\n"
            "- Do not output fake local-agent wrappers like '🤖 Delegated to ...' in normal chat mode."
        ),
        FILE_IO_RULES,
    ]

    if memories_text:
        parts.append(memories_text)

    if session_summary:
        parts.append(f"## Summary of Previous Conversation\n\n{session_summary}")

    if skills_text:
        parts.append(skills_text)

    return "\n\n---\n\n".join(parts)
