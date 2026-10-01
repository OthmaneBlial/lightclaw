from __future__ import annotations

import json
import os
import sys
import time
from types import SimpleNamespace

from config import Config
from core.bot.delegation.doctor import DelegationDoctorMixin
from core.doctor import build_doctor_report, render_doctor_text
from lightclaw_cli import cmd_doctor


def test_doctor_report_is_secret_safe_and_shows_access_policy(tmp_path, monkeypatch):
    monkeypatch.setattr("core.doctor.sys", SimpleNamespace(version_info=(3, 14)))
    config_file = tmp_path / "config.env"
    config_file.write_text("OPENAI_API_KEY=super-secret-value\n", encoding="utf-8")
    config_file.chmod(0o600)
    config = Config(
        config_path=str(config_file),
        workspace_path=str(tmp_path / "workspace"),
        telegram_allowed_users=["123"],
        telegram_bot_token="123456789:telegram-secret-value-long",
        llm_provider="openai",
        openai_api_key="super-secret-value",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-value")

    report = build_doctor_report(config)
    serialized = json.dumps(report)

    assert report["overall"] in {"ok", "warning"}
    assert report["lightclaw"]["python_supported"] is True
    assert report["lightclaw"]["access_policy"] == (
        "restricted (1 allowed user(s); private chats only)"
    )
    assert "super-secret-value" not in serialized
    assert "telegram-secret-value" not in serialized
    assert "Access policy: restricted" in render_doctor_text(report)


def test_doctor_json_command_fails_closed_without_config(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    code = cmd_doctor(SimpleNamespace(home=str(tmp_path), json=True))
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["overall"] == "error"
    assert payload["lightclaw"]["access_policy"] == "blocked (no owner configured)"


def test_doctor_reports_missing_optional_provider_sdk(tmp_path, monkeypatch):
    config_file = tmp_path / "config.env"
    config_file.write_text("LLM_PROVIDER=gemini\n", encoding="utf-8")
    config_file.chmod(0o600)
    config = Config(
        config_path=str(config_file),
        workspace_path=str(tmp_path / "workspace"),
        telegram_allowed_users=["123"],
        telegram_bot_token="123456789:test-token-long-enough",
        llm_provider="gemini",
        gemini_api_key="test-provider-secret",
    )
    monkeypatch.setattr("core.doctor.provider_sdk_available", lambda _provider: False)

    report = build_doctor_report(config)
    provider_check = next(
        item for item in report["checks"] if item["name"] == "provider_sdk"
    )

    assert report["overall"] == "error"
    assert provider_check["status"] == "error"
    assert "lightclaw-ai[gemini]" in provider_check["detail"]


def test_agent_doctor_timeout_kills_probe_process_group(tmp_path):
    marker = tmp_path / "child-survived-timeout"
    child_code = (
        "import pathlib,time; time.sleep(1.5); "
        f"pathlib.Path({str(marker)!r}).write_text('alive')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        "time.sleep(30)"
    )

    result = DelegationDoctorMixin()._run_probe_command(
        [sys.executable, "-c", parent_code], timeout_sec=1
    )
    time.sleep(1)

    assert result["timed_out"] is True
    assert result["exit_code"] == 124
    if os.name == "posix":
        assert not marker.exists()
