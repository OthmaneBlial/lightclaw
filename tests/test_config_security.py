from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from config import Config, load_config
from core.bot.base import BotBaseMixin
from core.security import (
    access_policy_label,
    delegated_process_env,
    has_sensitive_content,
    is_sensitive_path,
    redact_text,
)


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


@pytest.mark.parametrize(
    "name",
    ["PROVIDER_TIMEOUT_SEC", "MEMORY_TOP_K", "LOCAL_AGENT_MULTI_REPAIR_ATTEMPTS"],
)
def test_invalid_integer_setting_names_the_configuration_key(monkeypatch, name):
    monkeypatch.setenv(name, "not-an-integer")

    with pytest.raises(ValueError, match=f"^{name} must be an integer$") as exc:
        load_config()
    assert "not-an-integer" not in str(exc.value)


def test_bot_authorization_fails_closed_and_honors_allowlist():
    bot = BotBaseMixin.__new__(BotBaseMixin)
    bot.config = Config()

    assert bot.is_allowed(123) is False

    bot.config.telegram_public_bot_ack = True
    assert bot.is_allowed(123) is True

    bot.config.telegram_allowed_users = ["99"]
    assert bot.is_allowed(99) is True
    assert bot.is_allowed(123) is False


def test_update_authorization_restricts_allowlisted_users_to_private_chats():
    bot = BotBaseMixin.__new__(BotBaseMixin)
    bot.config = Config(telegram_allowed_users=["123"])
    user = SimpleNamespace(id=123)
    private = SimpleNamespace(
        effective_user=user, effective_chat=SimpleNamespace(type="private")
    )
    group = SimpleNamespace(
        effective_user=user, effective_chat=SimpleNamespace(type="group")
    )
    missing_chat = SimpleNamespace(effective_user=user, effective_chat=None)

    assert bot.is_update_allowed(private) is True
    assert bot.is_update_allowed(group) is False
    assert bot.is_update_allowed(missing_chat) is False


def test_public_override_allows_group_chats_but_allowlist_does_not():
    bot = BotBaseMixin.__new__(BotBaseMixin)
    group = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(type="supergroup"),
    )

    bot.config = Config(telegram_public_bot_ack=True)
    assert bot.is_update_allowed(group) is True

    bot.config = Config(
        telegram_allowed_users=["123"], telegram_public_bot_ack=True
    )
    assert bot.is_update_allowed(group) is False


def test_access_policy_labels_do_not_expose_user_ids():
    assert access_policy_label([], False) == "blocked (no owner configured)"
    assert access_policy_label([], True) == "public (explicit override)"
    assert access_policy_label(["123", "456"], False) == (
        "restricted (2 allowed user(s); private chats only)"
    )


def test_privileged_rate_limiter_uses_per_user_and_action_windows(monkeypatch):
    bot = BotBaseMixin.__new__(BotBaseMixin)
    bot._privileged_request_times = {}
    clock = iter([10.0, 11.0, 12.0, 80.0])
    monkeypatch.setattr("core.bot.base.time.monotonic", lambda: next(clock))

    assert bot._privileged_rate_limited(7, "agent", limit=2, window_sec=60) is False
    assert bot._privileged_rate_limited(7, "agent", limit=2, window_sec=60) is False
    assert bot._privileged_rate_limited(7, "agent", limit=2, window_sec=60) is True
    assert bot._privileged_rate_limited(7, "agent", limit=2, window_sec=60) is False


def test_privileged_rate_limiter_bounds_public_user_churn(monkeypatch):
    monkeypatch.setattr("core.bot.base.MAX_PRIVILEGED_RATE_LIMIT_KEYS", 8)
    monkeypatch.setattr("core.bot.base.time.monotonic", lambda: 10.0)
    bot = BotBaseMixin.__new__(BotBaseMixin)
    bot._privileged_request_times = {}

    for user_id in range(12):
        assert bot._privileged_rate_limited(user_id, "agent", limit=2) is False

    assert len(bot._privileged_request_times) == 8
    assert bot._privileged_rate_limited(4, "agent", limit=2) is False
    assert bot._privileged_rate_limited(12, "agent", limit=2) is False
    assert ("5", "agent") not in bot._privileged_request_times
    assert bot._privileged_rate_limited(4, "agent", limit=2) is True


def test_workspace_candidates_rank_newest_files(tmp_path):
    bot = BotBaseMixin.__new__(BotBaseMixin)
    bot.config = SimpleNamespace(workspace_path=str(tmp_path))
    bot._last_file_by_session = {}
    for index in range(25):
        path = tmp_path / f"file-{index:02}.txt"
        path.write_text("fixture\n", encoding="utf-8")
        os.utime(path, (index, index))

    assert bot._collect_workspace_candidates("", "chat-1", limit=3) == [
        "file-24.txt",
        "file-23.txt",
        "file-22.txt",
    ]


def test_recent_credentials_do_not_hide_safe_workspace_candidates(tmp_path):
    bot = BotBaseMixin.__new__(BotBaseMixin)
    bot.config = SimpleNamespace(workspace_path=str(tmp_path))
    bot._last_file_by_session = {}
    for index in range(25):
        path = tmp_path / f".env.secret-{index:02}.local"
        path.write_text("TOKEN=private\n", encoding="utf-8")
        os.utime(path, (100 + index, 100 + index))
    (tmp_path / "safe.txt").write_text("safe\n", encoding="utf-8")
    os.utime(tmp_path / "safe.txt", (1, 1))

    assert bot._collect_workspace_candidates("", "chat-1", limit=1) == ["safe.txt"]


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
    assert "nested-secret" not in redact_text("note=prefix:API_KEY=nested-secret")


def test_redaction_handles_large_nonsecret_text():
    text = "x" * 500_000

    assert redact_text(text) == text


def test_sensitive_context_detection_covers_credential_paths_and_json_keys():
    assert is_sensitive_path(".env.local")
    assert is_sensitive_path(".ssh/id_ed25519")
    assert is_sensitive_path("config/credentials.json")
    assert not is_sensitive_path("src/auth.py")
    assert has_sensitive_content('{"apiKey": "json-secret"}')
    assert has_sensitive_content('AWS_ACCESS_KEY_ID="AKIAEXAMPLE"')
    assert not has_sensitive_content('print("ordinary code")')
    assert "json-secret" not in redact_text('{"apiKey": "json-secret"}')
