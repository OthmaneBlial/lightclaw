"""Logging configuration for LightClaw."""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .security import redact_text

_JSON_LOG_MAX_BYTES = 5 * 1024 * 1024
_JSON_LOG_BACKUP_COUNT = 3
_JSON_LOG_FIELD_MAX_CHARS = 4096


class _SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        known_values = os.environ
        record.msg = redact_text(record.getMessage(), known_values)
        record.args = ()
        if record.exc_info:
            record.exc_text = redact_text(
                logging.Formatter().formatException(record.exc_info), known_values
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_text(record.exc_text, known_values)
        return True


class _PrivateFileHandler(RotatingFileHandler):
    def _open(self):
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(self.baseFilename, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"JSONL log path is not a regular file: {self.baseFilename}")
            os.fchmod(fd, 0o600)
        except BaseException:
            os.close(fd)
            raise
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lightclaw")
for handler in logging.getLogger().handlers:
    handler.addFilter(_SecretRedactionFilter())

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

        if session_id and len(session_id) > _JSON_LOG_FIELD_MAX_CHARS:
            session_id = session_id[:_JSON_LOG_FIELD_MAX_CHARS] + "…[truncated]"
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": body,
            "session": session_id,
            "channel": _infer_channel(session_id),
            "operation": _infer_operation(body),
        }
        if record.exc_info or record.exc_text:
            payload["exception"] = record.exc_text or self.formatException(record.exc_info)

        for field in ("message", "exception"):
            value = payload.get(field)
            if isinstance(value, str) and len(value) > _JSON_LOG_FIELD_MAX_CHARS:
                payload[field] = value[:_JSON_LOG_FIELD_MAX_CHARS] + "…[truncated]"

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

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    file_handler = _PrivateFileHandler(
        path,
        mode="a",
        encoding="utf-8",
        maxBytes=_JSON_LOG_MAX_BYTES,
        backupCount=_JSON_LOG_BACKUP_COUNT,
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(_JsonLogFormatter())
    file_handler.addFilter(_SecretRedactionFilter())
    logger.addHandler(file_handler)
    logger.info(f"Structured JSON logging enabled: {path.as_posix()}")
    return path
