"""Runtime path, personality loading, and system prompt construction."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from config import Config

from .constants import FALLBACK_IDENTITY, FILE_IO_RULES, PROJECT_ROOT


def resolve_runtime_path(path_value: str) -> Path:
    """Resolve configured paths relative to LIGHTCLAW_HOME or project root."""
    runtime_home = os.getenv("LIGHTCLAW_HOME", "").strip()
    base_dir = Path(runtime_home).expanduser().resolve() if runtime_home else PROJECT_ROOT
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def load_personality(workspace_path: str) -> str:
    """Load personality from runtime workspace files (SOUL.md, IDENTITY.md, USER.md).

    Falls back to a hardcoded identity if no files exist.
    """
    files = ["IDENTITY.md", "SOUL.md", "USER.md"]
    parts = []

    for filename in files:
        filepath = Path(workspace_path) / filename
        if filepath.exists():
            try:
                content = filepath.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(content)
            except Exception:
                pass

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
