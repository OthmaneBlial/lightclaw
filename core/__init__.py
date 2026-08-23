"""LightClaw core package with lazy public imports."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "_escape_html": (".markdown", "_escape_html"),
    "build_system_prompt": (".personality", "build_system_prompt"),
    "FALLBACK_IDENTITY": (".constants", "FALLBACK_IDENTITY"),
    "FileOperationResult": (".types", "FileOperationResult"),
    "FILE_IO_RULES": (".constants", "FILE_IO_RULES"),
    "LightClawBot": (".bot", "LightClawBot"),
    "load_personality": (".personality", "load_personality"),
    "log": (".logging_setup", "log"),
    "main": (".app", "main"),
    "markdown_to_telegram_html": (".markdown", "markdown_to_telegram_html"),
    "PROJECT_ROOT": (".constants", "PROJECT_ROOT"),
    "resolve_runtime_path": (".personality", "resolve_runtime_path"),
    "STRICT_LOCAL_AGENT_DENY_PATTERNS": (
        ".constants",
        "STRICT_LOCAL_AGENT_DENY_PATTERNS",
    ),
    "transcribe_voice": (".voice", "transcribe_voice"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value
