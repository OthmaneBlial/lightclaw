"""Local coding-agent delegation and doctor/check utilities."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ..logging_setup import log
from ..markdown import _escape_html


class BotDelegationMixin:
    @staticmethod
    def _agent_aliases() -> dict[str, str]:
        return {
            "codex": "codex",
            "codex-cli": "codex",
            "claude": "claude",
            "claude-code": "claude",
            "opencode": "opencode",
            "open-code": "opencode",
            "open_code": "opencode",
        }

    def _available_local_agents(self) -> dict[str, str]:
        """Return locally available coding agents (name -> executable path)."""
        binaries = {
            "codex": "codex",
            "claude": "claude",
            "opencode": "opencode",
        }
        available: dict[str, str] = {}
        for name, binary in binaries.items():
            path = shutil.which(binary)
            if path:
                available[name] = path
        return available

    def _resolve_local_agent_name(self, raw_name: str) -> str | None:
        alias = self._agent_aliases().get((raw_name or "").strip().lower())
        if not alias:
            return None
        return alias

    @staticmethod
    def _agent_usage_text() -> str:
        return (
            "<b>Usage</b>\n"
            "<code>/agent</code> - show status + available local agents\n"
            "<code>/agent doctor</code> - run install/version/auth preflight checks\n"
            "<code>/agent use &lt;codex|claude|opencode&gt;</code> - route chat messages to that local agent\n"
            "<code>/agent off</code> - disable delegation mode for this chat\n"
            "<code>/agent run &lt;task&gt;</code> - run one task with current active agent\n"
            "<code>/agent run &lt;agent&gt; &lt;task&gt;</code> - one-shot with a specific agent"
        )

    def _render_agent_status(self, session_id: str) -> str:
        available = self._available_local_agents()
        active = self._agent_mode_by_session.get(session_id)

        lines = ["🤖 <b>Local Agent Delegation</b>", ""]
        if active:
            lines.append(f"<b>Active in this chat:</b> <code>{_escape_html(active)}</code>")
        else:
            lines.append("<b>Active in this chat:</b> none")
        lines.append("")

        if available:
            lines.append("<b>Installed local agents:</b>")
            for name in sorted(available):
                lines.append(
                    f"• <code>{_escape_html(name)}</code> "
                    f"({_escape_html(available[name])})"
                )
        else:
            lines.append("No supported local coding agents found in PATH.")

        lines.append("")
        lines.append(self._agent_usage_text())
        return "\n".join(lines)

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
        binary = {"codex": "codex", "claude": "claude", "opencode": "opencode"}[agent]
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

    @staticmethod
    def _resolve_opencode_auth_path() -> Path:
        custom_home = os.getenv("OPENCODE_HOME", "").strip()
        if custom_home:
            return Path(custom_home).expanduser() / "auth.json"
        xdg_data_home = os.getenv("XDG_DATA_HOME", "").strip()
        data_root = (
            Path(xdg_data_home).expanduser()
            if xdg_data_home
            else (Path.home() / ".local" / "share")
        )
        return data_root / "opencode" / "auth.json"

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

    def _opencode_doctor_auth_status(self) -> tuple[str, str, str]:
        list_probe = self._run_probe_command(["opencode", "auth", "list"], timeout_sec=10)
        combined = self._strip_ansi(
            "\n".join(
                part for part in [list_probe.get("stdout", ""), list_probe.get("stderr", "")]
                if part
            )
        ).strip()
        match = re.search(r"\b(\d+)\s+credentials?\b", combined.lower())
        if match:
            count = int(match.group(1))
            if count > 0:
                return ("ok", f"{count} credential(s) configured.", "")
            return ("error", "No OpenCode credentials configured.", "opencode auth login")

        auth_path = self._resolve_opencode_auth_path()
        if auth_path.exists():
            try:
                data = json.loads(auth_path.read_text(encoding="utf-8"))
                count = 0
                if isinstance(data, dict):
                    for item in data.values():
                        if not isinstance(item, dict):
                            continue
                        for key_name in ("key", "token", "access_token"):
                            value = item.get(key_name)
                            if isinstance(value, str) and value.strip():
                                count += 1
                                break
                if count > 0:
                    return ("ok", f"{count} credential(s) found in {auth_path.as_posix()}.", "")
                return (
                    "error",
                    f"{auth_path.as_posix()} exists but has no usable credentials.",
                    "opencode auth login",
                )
            except Exception:
                return (
                    "warn",
                    f"Could not parse {auth_path.as_posix()}.",
                    "opencode auth login",
                )

        if list_probe.get("timed_out"):
            return ("warn", "Auth list probe timed out.", "opencode auth login")

        hint = self._first_nonempty_line(combined)
        if hint:
            hint = f" ({hint[:120]})"
        return (
            "error",
            f"No OpenCode credentials detected{hint}.",
            "opencode auth login",
        )

    def _render_agent_doctor_report(self) -> str:
        """Run local delegation preflight checks for supported external agent CLIs."""
        available = self._available_local_agents()
        auth_checks = {
            "codex": self._codex_doctor_auth_status,
            "claude": self._claude_doctor_auth_status,
            "opencode": self._opencode_doctor_auth_status,
        }

        lines = [
            "🩺 <b>Local Agent Doctor</b>",
            "",
            "Legend: ✅ ready, ⚠️ attention needed, ❌ action required",
            "",
        ]

        for agent in ("codex", "claude", "opencode"):
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

    def _build_delegation_prompt(self, task: str) -> str:
        workspace = Path(self.config.workspace_path).resolve().as_posix()
        return (
            "You are a local coding agent delegated by LightClaw.\n"
            f"Workspace root: {workspace}\n\n"
            "Requirements:\n"
            "- Implement the task directly by creating/editing files in this workspace.\n"
            "- Do not ask for confirmation; make reasonable assumptions and proceed.\n"
            "- If the task is large, still perform as much as possible in one run.\n"
            "- Do not dump full source files in the final response.\n"
            "- End with a concise summary of what was created/updated.\n\n"
            "TASK:\n"
            f"{task}\n"
        )

    def _snapshot_workspace_state(self) -> dict[str, tuple[int, int]]:
        """Snapshot workspace file metadata for before/after change detection."""
        workspace = Path(self.config.workspace_path).resolve()
        snapshot: dict[str, tuple[int, int]] = {}
        for path in workspace.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except Exception:
                continue
            rel = path.relative_to(workspace).as_posix()
            snapshot[rel] = (int(stat.st_size), int(stat.st_mtime_ns))
        return snapshot

    @staticmethod
    def _summarize_workspace_delta(
        before: dict[str, tuple[int, int]],
        after: dict[str, tuple[int, int]],
        max_items_per_group: int = 12,
    ) -> str:
        before_paths = set(before.keys())
        after_paths = set(after.keys())

        created = sorted(after_paths - before_paths)
        deleted = sorted(before_paths - after_paths)
        updated = sorted(
            path for path in (before_paths & after_paths) if before[path] != after[path]
        )

        total = len(created) + len(updated) + len(deleted)
        if total == 0:
            return "No workspace file changes detected."

        lines = [
            "✅ Workspace changes detected:",
            f"- Created: {len(created)}",
            f"- Updated: {len(updated)}",
            f"- Deleted: {len(deleted)}",
        ]

        for label, items in (("Created", created), ("Updated", updated), ("Deleted", deleted)):
            if not items:
                continue
            for path in items[:max_items_per_group]:
                lines.append(f"- {label}: `{path}`")
            remaining = len(items) - max_items_per_group
            if remaining > 0:
                lines.append(f"- {label}: ... and {remaining} more")

        return "\n".join(lines)

    @staticmethod
    def _strip_ansi(text: str) -> str:
        return re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", text or "")

    @staticmethod
    def _compact_external_agent_summary(text: str, max_chars: int = 900) -> str:
        raw = (text or "").strip()
        if not raw:
            return ""
        compact = re.sub(r"```[\s\S]*?```", "", raw)
        compact = re.sub(r"\n{3,}", "\n\n", compact).strip()
        if len(compact) > max_chars:
            compact = compact[:max_chars].rstrip() + "..."
        return compact

    def _parse_codex_exec_output(self, stdout: str) -> str:
        parts: list[str] = []
        last_error = ""
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            event_type = str(obj.get("type") or "")
            if event_type == "item.completed":
                item = obj.get("item") or {}
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text = str(item.get("text") or "").strip()
                    if text:
                        parts.append(text)
            elif event_type == "error":
                last_error = str(obj.get("message") or last_error)
            elif event_type == "turn.failed":
                err = obj.get("error") or {}
                if isinstance(err, dict):
                    last_error = str(err.get("message") or last_error)

        if parts:
            return "\n".join(parts).strip()
        if last_error:
            return f"Error: {last_error}"
        return (stdout or "").strip()[-2000:]

    def _parse_claude_cli_output(self, stdout: str) -> str:
        cleaned = self._strip_ansi(stdout).strip()
        if not cleaned:
            return ""

        parsed_obj = None
        try:
            parsed_obj = json.loads(cleaned)
        except Exception:
            for line in reversed(cleaned.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed_obj = json.loads(line)
                    break
                except Exception:
                    continue

        if isinstance(parsed_obj, dict):
            result = str(parsed_obj.get("result") or "").strip()
            if result:
                return result
            msg = str(parsed_obj.get("message") or "").strip()
            if msg:
                return msg

        return cleaned[-2000:]

    def _parse_opencode_run_output(self, stdout: str) -> str:
        cleaned = self._strip_ansi(stdout).strip()
        if not cleaned:
            return ""

        pieces: list[str] = []
        last_error = ""

        def add_piece(value: str):
            text = (value or "").strip()
            if text and text not in pieces:
                pieces.append(text)

        for line in cleaned.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                add_piece(line)
                continue

            event_type = str(obj.get("type") or "")
            if event_type == "error":
                err = obj.get("error") or {}
                if isinstance(err, dict):
                    data = err.get("data") or {}
                    if isinstance(data, dict):
                        last_error = str(data.get("message") or last_error)
                    if not last_error:
                        last_error = str(err.get("message") or last_error)
                if not last_error:
                    last_error = str(obj.get("message") or "unknown error")
                continue

            for key in ("result", "message", "content", "text"):
                val = obj.get(key)
                if isinstance(val, str):
                    add_piece(val)

            msg = obj.get("message")
            if isinstance(msg, dict):
                for key in ("text", "content", "result"):
                    val = msg.get(key)
                    if isinstance(val, str):
                        add_piece(val)
                content = msg.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, str):
                            add_piece(part)
                        elif isinstance(part, dict):
                            for key in ("text", "content", "value"):
                                val = part.get(key)
                                if isinstance(val, str):
                                    add_piece(val)

        if pieces:
            return "\n".join(pieces).strip()
        if last_error:
            return f"Error: {last_error}"
        return cleaned[-2000:]

    def _invoke_local_agent_sync(self, agent: str, task: str) -> dict:
        workspace = Path(self.config.workspace_path).resolve()
        timeout_sec = max(60, int(self.config.local_agent_timeout_sec))
        prompt = self._build_delegation_prompt(task)
        env = os.environ.copy()
        env["LIGHTCLAW_DELEGATED_AGENT"] = "1"
        env["CI"] = "1"

        cmd: list[str]
        run_input: str | None = None
        if agent == "codex":
            cmd = [
                "codex",
                "exec",
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
                "--color",
                "never",
                "-C",
                workspace.as_posix(),
                "-",
            ]
            run_input = prompt
        elif agent == "claude":
            cmd = [
                "claude",
                "-p",
                "--output-format",
                "json",
                "--dangerously-skip-permissions",
                "--no-chrome",
                "--no-session-persistence",
                "-",
            ]
            run_input = prompt
        elif agent == "opencode":
            cmd = [
                "opencode",
                "run",
                "--format",
                "json",
                prompt,
            ]
        else:
            return {
                "ok": False,
                "exit_code": 1,
                "stdout": "",
                "stderr": f"unsupported local agent: {agent}",
                "summary": "",
                "elapsed": 0.0,
                "timed_out": False,
            }

        started = time.monotonic()
        try:
            completed = subprocess.run(
                cmd,
                input=run_input,
                text=True,
                capture_output=True,
                cwd=workspace.as_posix(),
                env=env,
                timeout=timeout_sec,
            )
            elapsed = time.monotonic() - started
        except subprocess.TimeoutExpired as e:
            elapsed = time.monotonic() - started
            return {
                "ok": False,
                "exit_code": 124,
                "stdout": str(e.stdout or ""),
                "stderr": (str(e.stderr or "") + f"\nTimed out after {timeout_sec}s").strip(),
                "summary": "",
                "elapsed": elapsed,
                "timed_out": True,
            }
        except Exception as e:
            elapsed = time.monotonic() - started
            return {
                "ok": False,
                "exit_code": 1,
                "stdout": "",
                "stderr": str(e),
                "summary": "",
                "elapsed": elapsed,
                "timed_out": False,
            }

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""

        if agent == "codex":
            summary = self._parse_codex_exec_output(stdout)
        elif agent == "claude":
            summary = self._parse_claude_cli_output(stdout)
        else:
            summary = self._parse_opencode_run_output(stdout)

        ok = completed.returncode == 0
        if summary.strip().lower().startswith("error:"):
            ok = False

        return {
            "ok": ok,
            "exit_code": int(completed.returncode if ok or completed.returncode != 0 else 1),
            "stdout": stdout,
            "stderr": stderr,
            "summary": summary,
            "elapsed": elapsed,
            "timed_out": False,
        }

    async def _run_local_agent_task(self, session_id: str, agent: str, task: str) -> str:
        available = self._available_local_agents()
        if agent not in available:
            installed = ", ".join(sorted(available.keys())) if available else "none"
            return (
                f"⚠️ Local agent `{agent}` is not available on this machine.\n"
                f"Installed agents: {installed}"
            )

        blocked_by = self._delegation_safety_block_reason(task)
        if blocked_by:
            log.warning(
                f"[{session_id}] Blocked delegated task by safety policy "
                f"(agent={agent}, pattern={blocked_by})"
            )
            return (
                "🛑 Delegation blocked by local safety policy.\n"
                "Reason: potentially destructive task pattern detected.\n"
                f"Matched rule: `{blocked_by}`\n"
                "If this is intentional, set `LOCAL_AGENT_SAFETY_MODE=off` and restart."
            )

        before = await asyncio.to_thread(self._snapshot_workspace_state)
        result = await asyncio.to_thread(self._invoke_local_agent_sync, agent, task)
        after = await asyncio.to_thread(self._snapshot_workspace_state)

        summary = self._compact_external_agent_summary(str(result.get("summary") or ""))
        delta_summary = self._summarize_workspace_delta(before, after)
        stderr_excerpt = self._compact_external_agent_summary(
            self._strip_ansi(str(result.get("stderr") or ""))
        )

        lines = [f"🤖 Delegated to `{agent}`"]
        if result.get("ok"):
            lines.append(f"✅ Finished in {float(result.get('elapsed', 0.0)):.1f}s")
        elif result.get("timed_out"):
            lines.append(
                f"⚠️ Timed out after {int(self.config.local_agent_timeout_sec)}s"
            )
        else:
            lines.append(
                f"⚠️ `{agent}` exited with code {int(result.get('exit_code', 1))}"
            )

        if summary:
            lines.append("")
            lines.append(summary)

        lines.append("")
        lines.append(delta_summary)

        if not result.get("ok") and stderr_excerpt:
            lines.append("")
            lines.append(f"stderr: {stderr_excerpt[:700]}")

        log.info(
            f"[{session_id}] Local agent {agent} finished "
            f"(ok={result.get('ok')}, exit={result.get('exit_code')}, "
            f"elapsed={float(result.get('elapsed', 0.0)):.1f}s)"
        )
        return "\n".join(lines).strip()

