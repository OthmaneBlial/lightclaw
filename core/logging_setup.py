"""Logging configuration for LightClaw."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lightclaw")

# Reduce noisy transport logs by default (can be re-enabled with LIGHTCLAW_VERBOSE_HTTP=1).
if os.getenv("LIGHTCLAW_VERBOSE_HTTP", "").strip().lower() not in {"1", "true", "yes"}:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai._base_client").setLevel(logging.WARNING)


_SESSION_RE = re.compile(r"^\[(?P<session>[^\]]+)\]\s*(?P<body>.*)$")


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "")
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _infer_channel(session_id: str | None) -> str:
    if not session_id:
        return "system"
    if session_id.lstrip("-").isdigit():
        return "telegram"
    return "cli"


def _infer_operation(text: str) -> str:
    lower = (text or "").lower()
    if lower.startswith("user:"):
        return "user_message"
    if lower.startswith("bot:"):
        return "assistant_message"
    if "llm response" in lower:
        return "llm_response"
    if lower.startswith("saved file:"):
        return "file_saved"
    if lower.startswith("updated file:"):
        return "file_updated"
    if lower.startswith("applied edit block:"):
        return "file_edit"
    if "heartbeat" in lower:
        return "heartbeat"
    if "cron" in lower:
        return "cron"
    return "general"


class _JsonLogFormatter(logging.Formatter):
    """Structured one-line JSON formatter."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        session_id: str | None = None
        body = message

        matched = _SESSION_RE.match(message or "")
        if matched:
            session_id = matched.group("session")
            body = matched.group("body")

        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": body,
            "session": session_id,
            "channel": _infer_channel(session_id),
            "operation": _infer_operation(body),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


def configure_optional_json_logging(runtime_root: str | Path | None = None) -> Path | None:
    """Enable optional JSONL file logging while keeping human logs on stdout.

    Controlled by env:
    - JSON_LOG_ENABLED=1|true|yes|on
    - JSON_LOG_PATH=<optional path, defaults to <runtime_root>/logs/lightclaw.jsonl>
    """
    if not _env_flag("JSON_LOG_ENABLED", default=False):
        return None

    runtime_base = Path(runtime_root).expanduser().resolve() if runtime_root else Path.cwd().resolve()
    home_raw = os.getenv("LIGHTCLAW_HOME", "").strip()
    home_base = Path(home_raw).expanduser().resolve() if home_raw else Path.cwd().resolve()
    raw_path = os.getenv("JSON_LOG_PATH", "").strip()
    if raw_path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (home_base / path).resolve()
    else:
        path = (runtime_base / "logs" / "lightclaw.jsonl").resolve()

    logger = logging.getLogger("lightclaw")
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).resolve() == path:
            return path

    path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(_JsonLogFormatter())
    logger.addHandler(file_handler)
    logger.info(f"Structured JSON logging enabled: {path.as_posix()}")
    return path
