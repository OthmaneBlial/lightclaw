"""LightClaw core package."""

from .app import main
from .bot import LightClawBot
from .constants import FALLBACK_IDENTITY, FILE_IO_RULES, PROJECT_ROOT, STRICT_LOCAL_AGENT_DENY_PATTERNS
from .logging_setup import log
from .markdown import _escape_html, markdown_to_telegram_html
from .personality import build_system_prompt, load_personality, resolve_runtime_path
from .types import FileOperationResult
from .voice import transcribe_voice

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
