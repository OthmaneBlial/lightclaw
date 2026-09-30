import json
import logging

import pytest

from core.logging_setup import configure_optional_json_logging


@pytest.fixture
def isolated_lightclaw_logger():
    logger = logging.getLogger("lightclaw")
    original_handlers = list(logger.handlers)
    original_level = logger.level
    yield logger
    for handler in list(logger.handlers):
        if handler not in original_handlers:
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(original_level)


def _clear_logging_environment(monkeypatch):
    for name in ("JSON_LOG_ENABLED", "JSON_LOG_PATH", "LIGHTCLAW_HOME"):
        monkeypatch.delenv(name, raising=False)


def test_json_logging_is_disabled_by_default(tmp_path, monkeypatch, isolated_lightclaw_logger):
    _clear_logging_environment(monkeypatch)
    before = list(isolated_lightclaw_logger.handlers)

    assert configure_optional_json_logging(tmp_path) is None
    assert isolated_lightclaw_logger.handlers == before


def test_jsonl_schema_channel_inference_and_idempotent_setup(
    tmp_path, monkeypatch, isolated_lightclaw_logger
):
    isolated_lightclaw_logger.setLevel(logging.INFO)
    _clear_logging_environment(monkeypatch)
    path = tmp_path / "private" / "events.jsonl"
    monkeypatch.setenv("JSON_LOG_ENABLED", "yes")
    monkeypatch.setenv("JSON_LOG_PATH", str(path))
    required_fields = {
        "ts",
        "level",
        "logger",
        "message",
        "session",
        "channel",
        "operation",
    }

    assert configure_optional_json_logging(tmp_path) == path
    assert configure_optional_json_logging(tmp_path) == path
    handlers = [
        handler
        for handler in isolated_lightclaw_logger.handlers
        if isinstance(handler, logging.FileHandler)
    ]
    assert len(handlers) == 1

    isolated_lightclaw_logger.info("[12345] user: inspect the diff")
    isolated_lightclaw_logger.info("[terminal] bot: tests passed")
    isolated_lightclaw_logger.info("system heartbeat complete")

    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert all(required_fields <= entry.keys() for entry in entries)
    assert [(entry["session"], entry["channel"], entry["operation"]) for entry in entries[1:]] == [
        ("12345", "telegram", "user_message"),
        ("terminal", "cli", "assistant_message"),
        (None, "system", "heartbeat"),
    ]


def test_jsonl_redacts_environment_secrets_from_messages_and_tracebacks(
    tmp_path, monkeypatch, isolated_lightclaw_logger, caplog
):
    _clear_logging_environment(monkeypatch)
    secret = "fixture-provider-secret"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("JSON_LOG_ENABLED", "1")
    monkeypatch.setenv("JSON_LOG_PATH", str(path))
    isolated_lightclaw_logger.setLevel(logging.INFO)
    caplog.set_level(logging.INFO, logger="lightclaw")
    configure_optional_json_logging(tmp_path)

    try:
        raise RuntimeError(f"provider rejected {secret}")
    except RuntimeError:
        isolated_lightclaw_logger.exception("Provider request failed with %s", secret)

    text = path.read_text(encoding="utf-8")
    entry = json.loads(text.splitlines()[-1])
    assert secret not in text
    assert secret not in caplog.text
    assert "[REDACTED]" in entry["message"]
    assert "[REDACTED]" in entry["exception"]
