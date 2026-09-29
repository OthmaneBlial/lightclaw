from __future__ import annotations

import os
from types import SimpleNamespace

from core.bot import LightClawBot


def test_local_cli_probe_gets_minimal_environment(tmp_path, monkeypatch):
    secret = "fixture-provider-secret-that-must-not-be-inherited"
    codex_home = tmp_path / ".codex"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    observed = {}
    monkeypatch.setattr(
        "core.bot.delegation.doctor.subprocess.run",
        lambda *_args, **kwargs: (
            observed.update(kwargs)
            or SimpleNamespace(returncode=0, stdout="codex 1.0", stderr="")
        ),
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
        "core.bot.delegation.doctor.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=f"Login probe failed: OPENAI_API_KEY={secret}",
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
