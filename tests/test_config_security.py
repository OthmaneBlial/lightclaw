from __future__ import annotations

from config import Config, load_config
from core.bot.base import BotBaseMixin
from core.security import access_policy_label, delegated_process_env, redact_text


def test_config_defaults_fail_closed_and_sandboxed():
    cfg = load_config()

    assert cfg.telegram_allowed_users == []
    assert cfg.telegram_public_bot_ack is False
    assert cfg.local_agent_safety_mode == "strict"
    assert cfg.local_agent_capability_profile == "workspace-write"


def test_config_parses_explicit_public_override(monkeypatch):
    monkeypatch.setenv("LIGHTCLAW_PUBLIC_BOT_ACK", "yes")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "123, -456, ignored")

    cfg = load_config()

    assert cfg.telegram_public_bot_ack is True
    assert cfg.telegram_allowed_users == ["123", "-456"]


def test_bot_authorization_fails_closed_and_honors_allowlist():
    bot = BotBaseMixin.__new__(BotBaseMixin)
    bot.config = Config()

    assert bot.is_allowed(123) is False

    bot.config.telegram_public_bot_ack = True
    assert bot.is_allowed(123) is True

    bot.config.telegram_allowed_users = ["99"]
    assert bot.is_allowed(99) is True
    assert bot.is_allowed(123) is False


def test_access_policy_labels_do_not_expose_user_ids():
    assert access_policy_label([], False) == "blocked (no owner configured)"
    assert access_policy_label([], True) == "public (explicit override)"
    assert access_policy_label(["123", "456"], False) == "restricted (2 allowed user(s))"


def test_privileged_rate_limiter_uses_per_user_and_action_windows(monkeypatch):
    bot = BotBaseMixin.__new__(BotBaseMixin)
    bot._privileged_request_times = {}
    clock = iter([10.0, 11.0, 12.0, 80.0])
    monkeypatch.setattr("core.bot.base.time.monotonic", lambda: next(clock))

    assert bot._privileged_rate_limited(7, "agent", limit=2, window_sec=60) is False
    assert bot._privileged_rate_limited(7, "agent", limit=2, window_sec=60) is False
    assert bot._privileged_rate_limited(7, "agent", limit=2, window_sec=60) is True
    assert bot._privileged_rate_limited(7, "agent", limit=2, window_sec=60) is False


def test_delegated_environment_is_allowlisted_and_secret_free():
    source = {
        "PATH": "/usr/bin",
        "HOME": "/tmp/user",
        "LANG": "en_US.UTF-8",
        "OPENAI_API_KEY": "sk-secret",
        "TELEGRAM_BOT_TOKEN": "123456789:abcdefghijklmnopqrstuvwxyzABCDE",
        "RAILWAY_TOKEN": "railway-secret",
        "UNRELATED": "private-value",
    }

    result = delegated_process_env(
        source,
        extra={"CI": "1", "ANOTHER_SECRET": "blocked", "RUN_ID": "safe"},
    )

    assert result["PATH"] == "/usr/bin"
    assert result["HOME"] == "/tmp/user"
    assert result["CI"] == "1"
    assert result["RUN_ID"] == "safe"
    assert result["LIGHTCLAW_DELEGATED"] == "1"
    assert "OPENAI_API_KEY" not in result
    assert "TELEGRAM_BOT_TOKEN" not in result
    assert "RAILWAY_TOKEN" not in result
    assert "UNRELATED" not in result
    assert "ANOTHER_SECRET" not in result


def test_redaction_covers_assignments_bearer_tokens_and_known_values():
    raw = (
        "OPENAI_API_KEY=sk-live-secret "
        "Authorization: Bearer abcdefghijklmnop "
        "telegram 123456789:abcdefghijklmnopqrstuvwxyzABCDE "
        "custom-value"
    )
    redacted = redact_text(raw, {"CUSTOM_SECRET": "custom-value"})

    assert "sk-live-secret" not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "123456789:" not in redacted
    assert "custom-value" not in redacted
    assert redacted.count("REDACTED") >= 4
