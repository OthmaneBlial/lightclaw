from __future__ import annotations

import json
import os
import sys

import pytest

from core.bot import LightClawBot


def test_local_cli_probe_gets_minimal_environment(tmp_path, monkeypatch):
    secret = "fixture-provider-secret-that-must-not-be-inherited"
    codex_home = tmp_path / ".codex"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    bot = LightClawBot.__new__(LightClawBot)

    result = bot._run_probe_command(
        [
            sys.executable,
            "-c",
            "import json,os; print(json.dumps({'provider_key_was_inherited': "
            "bool(os.getenv('OPENAI_API_KEY')), 'CODEX_HOME': os.getenv('CODEX_HOME'), "
            "'CI': os.getenv('CI'), 'HOME': os.getenv('HOME')}))",
        ]
    )
    observed = json.loads(result["stdout"])

    assert result["ok"] is True
    assert observed["provider_key_was_inherited"] is False
    assert observed["CODEX_HOME"] == str(codex_home)
    assert observed["CI"] == "1"
    assert observed["HOME"] == os.environ.get("HOME")


def test_codex_doctor_redacts_secret_echoed_by_login_probe(monkeypatch):
    secret = "fixture-auth-token-that-must-not-leak"
    bot = LightClawBot.__new__(LightClawBot)
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    result = bot._run_probe_command(
        [
            sys.executable,
            "-c",
            "import sys; print(sys.argv[1], file=sys.stderr)",
            f"Login probe failed: OPENAI_API_KEY={secret}",
        ]
    )

    assert secret not in result["stderr"]
    assert "[REDACTED]" in result["stderr"]


def test_codex_doctor_treats_truncated_login_probe_as_unclear(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text('{"tokens":{"access_token":"fixture-token"}}')
    bot = LightClawBot.__new__(LightClawBot)
    bot._resolve_codex_auth_path = lambda: auth_path
    bot._run_probe_command = lambda *_args, **_kwargs: {
        "stdout": "Logged in",
        "stderr": "",
        "output_truncated": True,
        "timed_out": False,
    }

    status, message, _fix = bot._codex_doctor_auth_status()

    assert status == "warn"
    assert "status is unclear" in message


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
