"""Doctor and auth/version probe helpers for local agents."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time

from ...markdown import _escape_html


class DelegationDoctorMixin:
    @staticmethod
    def _first_nonempty_line(text: str) -> str:
        for line in (text or "").splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
        return ""

    @staticmethod
    def _format_age(seconds: float) -> str:
        if seconds < 60:
            return f"{int(seconds)}s"
        if seconds < 3600:
            return f"{int(seconds // 60)}m"
        if seconds < 86400:
            return f"{int(seconds // 3600)}h"
        return f"{int(seconds // 86400)}d"

    def _run_probe_command(
        self,
        cmd: list[str],
        timeout_sec: int = 8,
        input_text: str | None = None,
    ) -> dict:
        """Run a short-lived local CLI probe command."""
        env = os.environ.copy()
        env["CI"] = "1"
        try:
            completed = subprocess.run(
                cmd,
                input=input_text,
                text=True,
                capture_output=True,
                timeout=max(1, int(timeout_sec)),
                env=env,
            )
            return {
                "ok": completed.returncode == 0,
                "exit_code": int(completed.returncode),
                "stdout": str(completed.stdout or ""),
                "stderr": str(completed.stderr or ""),
                "timed_out": False,
                "error": "",
            }
        except subprocess.TimeoutExpired as e:
            return {
                "ok": False,
                "exit_code": 124,
                "stdout": str(e.stdout or ""),
                "stderr": str(e.stderr or ""),
                "timed_out": True,
                "error": f"timed out after {int(timeout_sec)}s",
            }
        except Exception as e:
            return {
                "ok": False,
                "exit_code": 1,
                "stdout": "",
                "stderr": "",
                "timed_out": False,
                "error": str(e),
            }

    def _probe_agent_version(self, agent: str) -> str:
        binary = {"codex": "codex", "claude": "claude"}[agent]
        probe = self._run_probe_command([binary, "--version"], timeout_sec=6)
        merged = self._strip_ansi(
            "\n".join(part for part in [probe.get("stdout", ""), probe.get("stderr", "")] if part)
        )
        line = self._first_nonempty_line(merged)
        if line:
            return line[:200]
        if probe.get("timed_out"):
            return "version check timed out"
        if probe.get("error"):
            return f"version check failed: {probe['error'][:120]}"
        return "unknown"

    @staticmethod
    def _resolve_codex_auth_path() -> Path:
        codex_home = os.getenv("CODEX_HOME", "").strip()
        if codex_home:
            return Path(codex_home).expanduser() / "auth.json"
        return Path.home() / ".codex" / "auth.json"

    @staticmethod
    def _resolve_claude_settings_paths() -> list[Path]:
        return [
            Path.home() / ".claude" / "settings.json",
            Path.home() / ".config" / "claude" / "settings.json",
        ]

    def _codex_doctor_auth_status(self) -> tuple[str, str, str]:
        auth_path = self._resolve_codex_auth_path()
        token_present = False
        path_parse_error = False
        age_seconds = 0.0
        age_known = False

        if auth_path.exists():
            try:
                payload = json.loads(auth_path.read_text(encoding="utf-8"))
                tokens = payload.get("tokens") if isinstance(payload, dict) else {}
                access_token = tokens.get("access_token") if isinstance(tokens, dict) else ""
                token_present = isinstance(access_token, str) and bool(access_token.strip())
            except Exception:
                path_parse_error = True

            try:
                age_seconds = max(0.0, time.time() - auth_path.stat().st_mtime)
                age_known = True
            except Exception:
                age_known = False

        login_probe = self._run_probe_command(["codex", "login", "status"], timeout_sec=8)
        login_text = self._strip_ansi(
            "\n".join(
                part
                for part in [login_probe.get("stdout", ""), login_probe.get("stderr", "")]
                if part
            )
        ).strip()
        login_text_lower = login_text.lower()
        logged_in = "logged in" in login_text_lower and "not logged" not in login_text_lower

        age_note = ""
        if age_known:
            age_note = f" (auth file age: {self._format_age(age_seconds)})"

        if logged_in and token_present:
            if age_known and age_seconds > 3600:
                return (
                    "warn",
                    f"Logged in, but auth file may be stale (>1h){age_note}.",
                    "codex login",
                )
            return (
                "ok",
                f"Logged in and access token found at {auth_path.as_posix()}{age_note}.",
                "",
            )

        if token_present and not logged_in:
            return (
                "warn",
                f"Token exists at {auth_path.as_posix()}, but login status probe was unclear.",
                "codex login status",
            )

        if path_parse_error:
            return (
                "warn",
                f"Could not parse {auth_path.as_posix()}.",
                "codex login",
            )

        if login_probe.get("timed_out"):
            return (
                "warn",
                "Login status probe timed out and no token file was found.",
                "codex login",
            )

        status_hint = self._first_nonempty_line(login_text)
        if status_hint:
            status_hint = f" ({status_hint[:120]})"
        return (
            "error",
            f"No valid Codex login detected{status_hint}.",
            "codex login",
        )

    def _claude_doctor_auth_status(self) -> tuple[str, str, str]:
        token_keys = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        for key in token_keys:
            if os.getenv(key, "").strip():
                return ("ok", f"{key} is set in process environment.", "")

        parse_errors: list[str] = []
        for settings_path in self._resolve_claude_settings_paths():
            if not settings_path.exists():
                continue
            try:
                data = json.loads(settings_path.read_text(encoding="utf-8"))
            except Exception:
                parse_errors.append(settings_path.as_posix())
                continue
            env_block = data.get("env") if isinstance(data, dict) else None
            if not isinstance(env_block, dict):
                continue
            for key in token_keys:
                value = env_block.get(key)
                if isinstance(value, str) and value.strip():
                    return ("ok", f"{key} found in {settings_path.as_posix()}.", "")

        if parse_errors:
            paths = ", ".join(parse_errors[:2])
            return (
                "warn",
                f"Could not parse Claude settings file(s): {paths}.",
                "claude setup-token",
            )

        return (
            "error",
            "No Claude auth token detected in env or Claude settings.",
            "claude setup-token",
        )

    def _render_agent_doctor_report(self) -> str:
        """Run local delegation preflight checks for supported external agent CLIs."""
        available = self._available_local_agents()
        auth_checks = {
            "codex": self._codex_doctor_auth_status,
            "claude": self._claude_doctor_auth_status,
        }

        lines = [
            "🩺 <b>Local Agent Doctor</b>",
            "",
            "Legend: ✅ ready, ⚠️ attention needed, ❌ action required",
            "",
        ]

        for agent in ("codex", "claude"):
            path = available.get(agent)
            if not path:
                lines.append(f"❌ <b>{_escape_html(agent)}</b>")
                lines.append("• Installed: no (not found in PATH)")
                lines.append(f"• Fix: install <code>{_escape_html(agent)}</code> and ensure it is on PATH")
                lines.append("")
                continue

            version = self._probe_agent_version(agent)
            status, auth_msg, fix = auth_checks[agent]()
            status_icon = {"ok": "✅", "warn": "⚠️", "error": "❌"}.get(status, "❓")

            lines.append(f"{status_icon} <b>{_escape_html(agent)}</b>")
            lines.append(f"• Path: <code>{_escape_html(path)}</code>")
            lines.append(f"• Version: <code>{_escape_html(version)}</code>")
            lines.append(f"• Auth: {_escape_html(auth_msg)}")
            if fix:
                lines.append(f"• Fix: <code>{_escape_html(fix)}</code>")
            lines.append("")

        lines.append("Run this before <code>/agent use ...</code> when delegation fails.")
        return "\n".join(lines).strip()
