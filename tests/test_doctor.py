from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from types import SimpleNamespace

import pytest

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


@pytest.mark.skipif(os.name != "posix", reason="detached pipe inheritance is POSIX-only")
def test_agent_doctor_timeout_does_not_wait_for_detached_child_pipes(tmp_path):
    pid_path = tmp_path / "detached-probe-child.pid"
    child_code = (
        "import os,pathlib,time; "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
        "time.sleep(20)"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}],start_new_session=True); "
        "time.sleep(30)"
    )
    result = {}
    errors = []
    finished = threading.Event()

    def run_probe():
        try:
            result["probe"] = DelegationDoctorMixin()._run_probe_command(
                [sys.executable, "-c", parent_code], timeout_sec=1
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=run_probe, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_path.exists(), "doctor probe did not start its detached child"
        assert finished.wait(timeout=2), "doctor waited for a detached child's output pipes"
    finally:
        if pid_path.exists():
            try:
                os.kill(int(pid_path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        thread.join(timeout=3)

    assert not thread.is_alive()
    assert not errors
    assert result["probe"]["timed_out"] is True
