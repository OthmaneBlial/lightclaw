from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from core.bot import LightClawBot


def test_local_cli_probe_gets_minimal_environment(tmp_path, monkeypatch):
    secret = "fixture-provider-secret-that-must-not-be-inherited"
    codex_home = tmp_path / ".codex"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    observed = {}

    def fake_popen(_cmd, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(
            pid=1,
            returncode=0,
            communicate=lambda **_kwargs: ("codex 1.0", ""),
        )

    monkeypatch.setattr(
        "core.bot.delegation.doctor.subprocess.Popen", fake_popen
    )
    bot = LightClawBot.__new__(LightClawBot)

    result = bot._run_probe_command(["codex", "--version"])

    assert result["ok"] is True
    assert "OPENAI_API_KEY" not in observed["env"]
    assert observed["env"]["CODEX_HOME"] == str(codex_home)
    assert observed["env"]["CI"] == "1"
    assert observed["env"].get("HOME") == os.environ.get("HOME")


def test_codex_doctor_redacts_secret_echoed_by_login_probe(tmp_path, monkeypatch):
    secret = "fixture-auth-token-that-must-not-leak"
    auth_path = tmp_path / "missing-auth.json"
    bot = LightClawBot.__new__(LightClawBot)
    bot._resolve_codex_auth_path = lambda: auth_path
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setattr(
        "core.bot.delegation.doctor.subprocess.Popen",
        lambda *_args, **_kwargs: SimpleNamespace(
            pid=1,
            returncode=1,
            communicate=lambda **_kwargs: (
                "", f"Login probe failed: OPENAI_API_KEY={secret}"
            ),
        ),
    )

    _status, message, _fix = bot._codex_doctor_auth_status()

    assert secret not in message
    assert "[REDACTED]" in message


def test_claude_doctor_skips_oversized_settings_file(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    settings_path.write_bytes(
        b'{"env":{"ANTHROPIC_API_KEY":"fixture-token"},"padding":"'
        + b"x" * (1024 * 1024)
        + b'"}'
    )
    bot = LightClawBot.__new__(LightClawBot)
    bot._resolve_claude_settings_paths = lambda: [settings_path]
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    status, message, _fix = bot._claude_doctor_auth_status()

    assert status == "warn"
    assert "Could not parse Claude settings" in message


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_agent_doctor_reports_recursively_nested_json_as_unreadable(
    tmp_path, monkeypatch, provider
):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}", encoding="utf-8")
    bot = LightClawBot.__new__(LightClawBot)

    def reject_nested_json(_content):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("core.bot.delegation.doctor.json.loads", reject_nested_json)
    if provider == "codex":
        bot._resolve_codex_auth_path = lambda: settings_path
        bot._run_probe_command = lambda *_args, **_kwargs: {
            "stdout": "Not logged in", "stderr": "", "timed_out": False,
        }
        status, message, _fix = bot._codex_doctor_auth_status()
    else:
        bot._resolve_claude_settings_paths = lambda: [settings_path]
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        status, message, _fix = bot._claude_doctor_auth_status()

    assert status == "warn"
    assert "Could not parse" in message
