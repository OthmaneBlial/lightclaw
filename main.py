#!/usr/bin/env python3
"""Compatibility facade for the modularized LightClaw core."""

from core import (
    _escape_html,
    build_system_prompt,
    FALLBACK_IDENTITY,
    FileOperationResult,
    FILE_IO_RULES,
    LightClawBot,
    load_personality,
    log,
    main,
    markdown_to_telegram_html,
    PROJECT_ROOT,
    resolve_runtime_path,
    STRICT_LOCAL_AGENT_DENY_PATTERNS,
    transcribe_voice,
)

__all__ = [
    "_escape_html",
    "build_system_prompt",
    "FALLBACK_IDENTITY",
    "FileOperationResult",
    "FILE_IO_RULES",
    "LightClawBot",
    "load_personality",
    "log",
    "main",
    "markdown_to_telegram_html",
    "PROJECT_ROOT",
    "resolve_runtime_path",
    "STRICT_LOCAL_AGENT_DENY_PATTERNS",
    "transcribe_voice",
]


if __name__ == "__main__":
    main()
