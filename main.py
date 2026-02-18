#!/usr/bin/env python3
"""
LightClaw — Minimal AI Agent for Telegram
==========================================
A minimalist Python AI agent. Telegram-only, 6 LLM providers, 
infinite memory, and customizable personality.

Architecture: Telegram Polling → handle_message → Memory Recall → LLM → Reply

Phase 2 features:
  - Markdown → HTML conversion for Telegram
  - "Thinking… 💭" placeholder with edit
  - Session summarization + context window management
  - Personality system (runtime workspace personality files)
  - Voice message transcription (Groq Whisper)
  - Photo/document handling
  - /show command + error handling
  - Orphan message cleanup
"""

import asyncio
import difflib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import Conflict, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Config, load_config
from memory import MemoryStore
from providers import LLMClient
from skills import SkillError, SkillManager

# Project root for resolving runtime-relative paths reliably.
PROJECT_ROOT = Path(__file__).resolve().parent

# ──────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lightclaw")


# ──────────────────────────────────────────────────────────────
# Markdown → Telegram HTML Converter
# ──────────────────────────────────────────────────────────────
# Robust Markdown-to-HTML converter for Telegram
# Telegram's HTML mode supports: <b>, <i>, <s>, <code>, <pre>, <a>


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def markdown_to_telegram_html(text: str) -> str:
    """Convert LLM markdown to Telegram-safe HTML.

    Handles code blocks, inline code, bold, italic, strikethrough,
    links, blockquotes, and list markers. All other text is HTML-escaped.
    """
    if not text:
        return ""

    # 1. Extract fenced code blocks → placeholders
    code_blocks: list[str] = []

    def _extract_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r"```\w*\n?([\s\S]*?)```", _extract_code_block, text)

    # 2. Extract inline code → placeholders
    inline_codes: list[str] = []

    def _extract_inline(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", _extract_inline, text)

    # 3. Strip heading markers (# Title → Title)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"\1", text, flags=re.MULTILINE)

    # 4. Strip blockquote markers
    text = re.sub(r"^>\s*(.*)$", r"\1", text, flags=re.MULTILINE)

    # 5. Escape HTML in remaining text
    text = _escape_html(text)

    # 6. Convert markdown formatting (order matters)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)  # links
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)  # bold
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)  # bold alt
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<i>\1</i>", text)  # italic
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)  # strikethrough
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)  # list markers

    # 7. Restore inline code
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00IC{i}\x00", f"<code>{_escape_html(code)}</code>")

    # 8. Restore code blocks
    for i, code in enumerate(code_blocks):
        text = text.replace(
            f"\x00CB{i}\x00", f"<pre><code>{_escape_html(code)}</code></pre>"
        )

    return text


def resolve_runtime_path(path_value: str) -> Path:
    """Resolve configured paths relative to LIGHTCLAW_HOME or project root."""
    runtime_home = os.getenv("LIGHTCLAW_HOME", "").strip()
    base_dir = (
        Path(runtime_home).expanduser().resolve()
        if runtime_home
        else PROJECT_ROOT
    )
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


# ──────────────────────────────────────────────────────────────
# File Operation Rules
# ──────────────────────────────────────────────────────────────

FILE_IO_RULES = """## File Operations (CRITICAL)
When creating NEW files (HTML, Python, CSS, Java, etc.):
- ALWAYS use this EXACT format: ```python:filename.py
- Put the filename AFTER the language with a colon: ```lang:filename.ext
- Examples: ```html:landing.html, ```python:script.py, ```css:style.css
- The file will be saved to the runtime workspace directory
- DO NOT include explanatory text inside code blocks - only the actual code
- If the output is large code, ALWAYS save to files and keep chat reply short.
- Never dump full source code in chat when files are created.

When editing EXISTING files:
- ALWAYS use this EXACT format:
```edit:path/to/file.ext
<<<<<<< SEARCH
exact old text from the file
=======
new text
>>>>>>> REPLACE
```
- SEARCH text must match the file exactly.
- SEARCH text must be unique (only one match).
- You may include multiple SEARCH/REPLACE hunks in one edit block.
- Keep file paths relative to the runtime workspace root.
- After edits, provide only a short summary (not full diff body)."""


# ──────────────────────────────────────────────────────────────
# Personality System (runtime workspace loader)
# ──────────────────────────────────────────────────────────────

FALLBACK_IDENTITY = f"""# LightClaw 🦞

You are LightClaw, a helpful, intelligent AI assistant with infinite memory.
You remember all past conversations and can recall context from previous sessions.

## Important Rules
1. Be helpful, accurate, and concise.
2. When you remember something from a past conversation, mention it naturally.
3. If you're unsure about a recalled memory, say so.
4. Respond in the same language the user writes in.

{FILE_IO_RULES}

## Example good response:
"Here's your landing page:
```html:index.html
<!DOCTYPE html>
<html>
...
</html>
```
Done! A modern book-selling landing page."
"""


def load_personality(workspace_path: str) -> str:
    """Load personality from runtime workspace files (SOUL.md, IDENTITY.md, USER.md).

    Falls back to a hardcoded identity if no files exist.
    """
    files = ["IDENTITY.md", "SOUL.md", "USER.md"]
    parts = []

    for filename in files:
        filepath = Path(workspace_path) / filename
        if filepath.exists():
            try:
                content = filepath.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(content)
            except Exception:
                pass

    if not parts:
        return FALLBACK_IDENTITY

    return "\n\n---\n\n".join(parts)


# ──────────────────────────────────────────────────────────────
# System Prompt Builder
# ──────────────────────────────────────────────────────────────


def build_system_prompt(
    config: Config,
    personality: str,
    memories_text: str,
    session_summary: str,
    skills_text: str = "",
) -> str:
    """Build the full system prompt with identity, memories, and summary."""
    parts = [
        personality,
        f"## Current Time\n{datetime.now().strftime('%Y-%m-%d %H:%M (%A)')}",
        f"## Provider\n{config.llm_provider} ({config.llm_model})",
        (
            "## Delegation Guardrails\n"
            "- Never claim or simulate local-agent execution unless LightClaw has already done it.\n"
            "- Do not output fake local-agent wrappers like '🤖 Delegated to ...' in normal chat mode."
        ),
        FILE_IO_RULES,
    ]

    if memories_text:
        parts.append(memories_text)

    if session_summary:
        parts.append(f"## Summary of Previous Conversation\n\n{session_summary}")

    if skills_text:
        parts.append(skills_text)

    return "\n\n---\n\n".join(parts)


# ──────────────────────────────────────────────────────────────
# Voice Transcription (Groq Whisper)
# ──────────────────────────────────────────────────────────────


async def transcribe_voice(audio_bytes: bytes, groq_api_key: str) -> str | None:
    """Transcribe audio using Groq's Whisper API. Returns text or None on failure."""
    if not groq_api_key:
        return None

    try:
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {groq_api_key}"},
                files={"file": ("audio.ogg", audio_bytes, "audio/ogg")},
                data={"model": "whisper-large-v3-turbo"},
            )
            if response.status_code == 200:
                return response.json().get("text", "")
    except ImportError:
        log.warning("httpx not installed — voice transcription unavailable. pip install httpx")
    except Exception as e:
        log.error(f"Voice transcription failed: {e}")

    return None


# ──────────────────────────────────────────────────────────────
# Telegram Bot Handlers
# ──────────────────────────────────────────────────────────────


@dataclass
class FileOperationResult:
    action: str
    path: str
    detail: str = ""
    diff: str = ""


class LightClawBot:
    """The main bot class wiring Telegram, Memory, and LLM together."""

    def __init__(self, config: Config):
        self.config = config
        self.memory = MemoryStore(config.memory_db_path)
        self.llm = LLMClient(config)
        self.skills = SkillManager(
            workspace_path=config.workspace_path,
            skills_state_path=config.skills_state_path,
            hub_base_url=config.skills_hub_base_url,
        )
        self.personality = load_personality(config.workspace_path)
        self.start_time = time.time()

        # Per-session summaries (in-memory, persisted via memory.py)
        self._session_summaries: dict[str, str] = {}
        # Lock to prevent concurrent summarization per session
        self._summarizing: set[str] = set()
        # Confirmation window for destructive memory wipe command (per chat).
        self._pending_wipe_confirm: dict[str, float] = {}
        # Track last successful file operation target per session.
        self._last_file_by_session: dict[str, str] = {}
        # Per-chat local delegation mode (codex/claude/opencode).
        self._agent_mode_by_session: dict[str, str] = {}
        # Backoff window to avoid repeated background LLM calls during provider failures.
        self._llm_backoff_until: float = 0.0
        # Throttle repeated Telegram polling conflict warnings.
        self._last_telegram_conflict_log_at: float = 0.0

    def is_allowed(self, user_id: int) -> bool:
        """Check if this user is in the allowlist (empty = allow all)."""
        if not self.config.telegram_allowed_users:
            return True
        return str(user_id) in self.config.telegram_allowed_users

    @staticmethod
    def _session_id_from_update(update: Update | None) -> str:
        if update and update.effective_chat:
            return str(update.effective_chat.id)
        return "unknown"

    @staticmethod
    def _trim_for_log(text: str, max_chars: int = 8000) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n...[truncated]"

    @staticmethod
    def _strip_html_for_log(text: str) -> str:
        return re.sub(r"<[^>]+>", "", text)

    def _log_user_message(self, session_id: str, text: str):
        log.info(f"[{session_id}] User: {self._trim_for_log(text)}")

    def _log_bot_message(self, session_id: str, text: str):
        log.info(f"[{session_id}] Bot: {self._trim_for_log(text)}")

    @staticmethod
    def _extract_file_mentions(text: str) -> list[str]:
        pattern = re.compile(r"\b([A-Za-z0-9._/-]+\.[A-Za-z0-9]{1,10})\b")
        return [m.group(1) for m in pattern.finditer(text or "")]

    def _is_file_intent(self, user_text: str) -> bool:
        lower = (user_text or "").lower()
        keywords = (
            "build", "create", "make", "generate", "landing page", "website",
            "html", "css", "javascript", "python", "script", "edit", "modify",
            "update", "improve", "enhance", "add more", "add feature", "refactor", "fix",
        )
        if any(k in lower for k in keywords):
            return True
        return bool(self._extract_file_mentions(user_text))

    @staticmethod
    def _is_deferral_response(text: str) -> bool:
        lower = (text or "").lower()
        patterns = (
            "let me first", "let me check", "let me read", "i'll first check",
            "i need to check", "i need to read", "before i", "then i'll",
            "i will check", "i'll inspect", "let me inspect",
        )
        return any(p in lower for p in patterns)

    @staticmethod
    def _is_provider_error_text(text: str) -> bool:
        lower = (text or "").strip().lower()
        if not lower:
            return False
        if lower.startswith("⚠️ error communicating with"):
            return True
        if lower.startswith("error communicating with"):
            return True
        return False

    def _llm_backoff_active(self) -> bool:
        return time.time() < self._llm_backoff_until

    def _set_llm_backoff(self, seconds: int = 180):
        duration = max(15, int(seconds))
        until = time.time() + duration
        if until > self._llm_backoff_until:
            self._llm_backoff_until = until
        log.warning(f"LLM backoff enabled for {duration}s due to provider errors")

    def _clear_llm_backoff(self):
        self._llm_backoff_until = 0.0

    def _llm_backoff_remaining_sec(self) -> int:
        return max(0, int(self._llm_backoff_until - time.time()))

    def _collect_workspace_candidates(self, user_text: str, session_id: str, limit: int = 4) -> list[str]:
        """Pick likely target files for forced edit passes."""
        candidates: list[str] = []

        # 1) Explicit file mention in user text.
        for mention in self._extract_file_mentions(user_text):
            target, rel_path, err = self._resolve_workspace_path(mention)
            if not err and target and rel_path:
                candidates.append(rel_path)

        # 2) Last touched file in this chat.
        last = self._last_file_by_session.get(session_id)
        if last:
            target, rel_path, err = self._resolve_workspace_path(last)
            if not err and target and rel_path and target.exists():
                candidates.append(rel_path)

        # 3) Most recently modified workspace files.
        workspace = Path(self.config.workspace_path).resolve()
        files = []
        for path in workspace.rglob("*"):
            if path.is_file():
                try:
                    files.append((path.stat().st_mtime, path))
                except Exception:
                    continue
        files.sort(reverse=True, key=lambda x: x[0])
        for _, path in files[:20]:
            rel = path.relative_to(workspace).as_posix()
            candidates.append(rel)
            if len(candidates) >= limit * 3:
                break

        # Unique preserving order, then limit.
        seen = set()
        unique: list[str] = []
        for item in candidates:
            if item in seen:
                continue
            seen.add(item)
            unique.append(item)
            if len(unique) >= limit:
                break
        return unique

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

    async def _reply_logged(
        self,
        update: Update,
        text: str,
        parse_mode: str | None = None,
    ):
        """Reply to Telegram and mirror the same content to terminal logs."""
        session_id = self._session_id_from_update(update)
        logged_text = self._strip_html_for_log(text) if parse_mode == ParseMode.HTML else text
        self._log_bot_message(session_id, logged_text)

        if parse_mode:
            return await update.message.reply_text(text, parse_mode=parse_mode)
        return await update.message.reply_text(text)

    # ── Token Estimation ─────────────────────────────────────

    @staticmethod
    def estimate_tokens(messages: list[dict]) -> int:
        """Estimate token count using a 2.5 chars/token heuristic."""
        total_chars = sum(len(m.get("content", "")) for m in messages)
        return total_chars * 2 // 5

    # ── Session Summarization ────────────────────────────────

    async def maybe_summarize(self, session_id: str):
        """Trigger summarization if history is too long or token count too high."""
        if self._llm_backoff_active():
            return

        recent = self.memory.get_recent(session_id, limit=100)
        recent = self._filter_recent_context(recent)
        token_estimate = self.estimate_tokens(recent)
        threshold = self.config.context_window * 75 // 100

        if len(recent) <= 20 and token_estimate <= threshold:
            return

        if session_id in self._summarizing:
            return
        self._summarizing.add(session_id)

        try:
            await self._summarize_session(session_id, recent)
        finally:
            self._summarizing.discard(session_id)

    async def _summarize_session(self, session_id: str, history: list[dict]):
        """Use the LLM to summarize older messages, keep last 4."""
        if len(history) <= 4:
            return

        to_summarize = history[:-4]

        # Filter to user/assistant only, skip oversized messages
        max_msg_tokens = self.config.context_window // 2
        valid = [
            m for m in to_summarize
            if m.get("role") in ("user", "assistant")
            and len(m.get("content", "")) * 2 // 5 <= max_msg_tokens
        ]

        if not valid:
            return

        existing_summary = self._sanitize_summary_for_prompt(
            self._session_summaries.get(session_id, "")
        )
        if self._is_provider_error_text(existing_summary):
            existing_summary = ""
            self._session_summaries.pop(session_id, None)

        # Build summarization prompt
        prompt = "Provide a concise summary of this conversation, preserving key context and important points.\n"
        if existing_summary:
            prompt += f"Existing context: {existing_summary}\n"
        prompt += "\nCONVERSATION:\n"
        for m in valid:
            prompt += f"{m['role']}: {m['content']}\n"

        try:
            summary = await self.llm.chat(
                [{"role": "user", "content": prompt}],
                system_prompt="You are a conversation summarizer. Be concise but preserve all important context.",
            )
            summary = self._sanitize_summary_for_prompt(summary)
            if summary and not self._is_provider_error_text(summary):
                self._session_summaries[session_id] = summary
                self._clear_llm_backoff()
                log.info(f"[{session_id}] Summarized {len(valid)} messages → {len(summary)} chars")
            elif summary and self._is_provider_error_text(summary):
                self._set_llm_backoff()
                log.warning(f"[{session_id}] Skipped summary update due to provider error response")
        except Exception as e:
            log.error(f"Summarization failed: {e}")

    def _get_session_summary(self, session_id: str) -> str:
        """Get the stored summary for a session."""
        # First check in-memory cache
        if session_id in self._session_summaries:
            summary = self._sanitize_summary_for_prompt(self._session_summaries[session_id])
            if self._is_provider_error_text(summary):
                self._session_summaries.pop(session_id, None)
                return ""
            return summary
        # Fall back to memory store
        summary = self._sanitize_summary_for_prompt(self.memory.get_summary(session_id))
        if self._is_provider_error_text(summary):
            return ""
        return summary

    # ── Emergency Context Compression ────────────────────────

    def _is_context_error(self, error_msg: str) -> bool:
        """Detect context window overflow errors from LLM providers."""
        lower = error_msg.lower()
        return any(kw in lower for kw in ("token", "context", "length", "too long", "too large"))

    # ── Orphan Tool Message Cleanup ──────────────────────────

    @staticmethod
    def _clean_orphan_messages(messages: list[dict]) -> list[dict]:
        """Strip leading 'tool' role messages that lack a preceding assistant tool call.

        # Prevent potential issue where tool roles are orphans
        """
        while messages and messages[0].get("role") == "tool":
            messages = messages[1:]
        return messages

    @staticmethod
    def _is_delegation_transcript_text(text: str) -> bool:
        """Detect local-agent transcript wrappers to keep them out of normal LLM context."""
        normalized = (text or "").strip()
        if not normalized:
            return False
        if normalized.startswith("🤖 Delegated to "):
            return True
        return (
            "No workspace file changes detected." in normalized
            and "Created/updated:" in normalized
        )

    def _filter_recent_context(self, messages: list[dict]) -> list[dict]:
        """Remove delegation transcripts and /agent command noise from recent history."""
        filtered: list[dict] = []
        for msg in messages:
            role = (msg.get("role") or "").strip()
            content = msg.get("content", "")
            if role == "assistant" and self._is_delegation_transcript_text(content):
                continue
            if role == "user" and content.strip().lower().startswith("/agent"):
                continue
            filtered.append(msg)
        return filtered

    def _filter_recalled_memories(self, memories: list) -> list:
        """Remove recalled snippets that can trigger fake delegation-style replies."""
        filtered = []
        for rec in memories:
            if rec.role == "assistant" and self._is_delegation_transcript_text(rec.content):
                continue
            if rec.role == "user" and rec.content.strip().lower().startswith("/agent"):
                continue
            filtered.append(rec)
        return filtered

    def _sanitize_summary_for_prompt(self, summary: str) -> str:
        """Strip delegation transcript artifacts from persistent session summaries."""
        if not summary:
            return ""
        if self._is_delegation_transcript_text(summary):
            return ""
        cleaned_lines: list[str] = []
        for line in summary.splitlines():
            stripped = line.strip()
            if (
                stripped.startswith("🤖 Delegated to ")
                or "No workspace file changes detected." in stripped
                or stripped.startswith("Created/updated:")
            ):
                continue
            cleaned_lines.append(line)
        return "\n".join(cleaned_lines).strip()

    # ── /start ────────────────────────────────────────────────

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        session_id = self._session_id_from_update(update)
        self._log_user_message(session_id, "/start")
        await self._reply_logged(
            update,
            "🦞 <b>LightClaw</b> is ready!\n\n"
            "I'm your AI assistant with infinite memory. "
            "I remember everything we've talked about, even across sessions.\n\n"
            "<b>Commands:</b>\n"
            "/help - Show this message\n"
            "/clear - Reset our conversation\n"
            "/wipe_memory - Wipe ALL memory (with confirmation)\n"
            "/memory - Show memory stats\n"
            "/recall &lt;query&gt; - Search my memories\n"
            "/skills - Manage skills (install/use/create)\n"
            "/agent - Delegate tasks to local coding agents\n"
            "/agent doctor - Check local agent install/auth health\n"
            "/show - Show current config",
            parse_mode=ParseMode.HTML,
        )

    # ── /help ─────────────────────────────────────────────────

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        session_id = self._session_id_from_update(update)
        self._log_user_message(session_id, "/help")
        await self._reply_logged(
            update,
            "🦞 <b>LightClaw Commands</b>\n\n"
            "/start - Welcome message\n"
            "/help - This help message\n"
            "/clear - Clear conversation history\n"
            "/wipe_memory - Wipe ALL memory (dangerous)\n"
            "/memory - Show memory statistics\n"
            "/recall &lt;query&gt; - Search past conversations\n"
            "/skills - Install/use/create skills\n"
            "/agent - Delegate tasks to local coding agents\n"
            "/agent doctor - Check local agent install/auth health\n"
            "/show - Show current model, provider, uptime",
            parse_mode=ParseMode.HTML,
        )

    # ── /clear ────────────────────────────────────────────────

    async def cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        session_id = str(update.effective_chat.id) if update.effective_chat else "unknown"
        self._log_user_message(session_id, "/clear")
        self.memory.clear_session(session_id)
        self._session_summaries.pop(session_id, None)
        await self._reply_logged(
            update,
            "🗑️ Conversation cleared. Your memories from this chat have been reset.\n"
            "Note: memories from other chats are preserved."
        )

    # ── /wipe_memory ─────────────────────────────────────────

    async def cmd_wipe_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Dangerous command: wipe all memory after explicit confirmation."""
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        session_id = str(update.effective_chat.id) if update.effective_chat else "unknown"
        args = [a.strip().lower() for a in (context.args or []) if a.strip()]
        self._log_user_message(session_id, f"/wipe_memory {' '.join(args)}".strip())

        now = time.time()
        confirm_window_sec = 90
        pending_until = self._pending_wipe_confirm.get(session_id, 0.0)

        if args and args[0] in {"confirm", "yes", "now"}:
            if pending_until and now <= pending_until:
                await asyncio.to_thread(self.memory.clear_all)
                self._session_summaries.clear()
                self._pending_wipe_confirm.pop(session_id, None)
                await self._reply_logged(
                    update,
                    "🧨 <b>All memory wiped.</b>\n"
                    "All sessions/interactions were deleted. The bot now starts fresh.",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await self._reply_logged(
                    update,
                    "No active wipe confirmation.\n"
                    "Run <code>/wipe_memory</code> first, then confirm within 90s with "
                    "<code>/wipe_memory confirm</code>.",
                    parse_mode=ParseMode.HTML,
                )
            return

        self._pending_wipe_confirm[session_id] = now + confirm_window_sec
        await self._reply_logged(
            update,
            "⚠️ <b>Danger: wipe ALL memory</b>\n"
            "This deletes every saved interaction and session across all chats.\n\n"
            f"To confirm within {confirm_window_sec}s, run:\n"
            "<code>/wipe_memory confirm</code>",
            parse_mode=ParseMode.HTML,
        )

    # ── /memory ───────────────────────────────────────────────

    async def cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        session_id = self._session_id_from_update(update)
        self._log_user_message(session_id, "/memory")
        stats = self.memory.stats()
        await self._reply_logged(
            update,
            f"🧠 <b>Memory Stats</b>\n\n"
            f"📝 Total interactions: {stats['total_interactions']}\n"
            f"💬 Unique sessions: {stats['unique_sessions']}\n"
            f"📚 Vocabulary size: {stats['vocabulary_size']}",
            parse_mode=ParseMode.HTML,
        )

    # ── /recall ───────────────────────────────────────────────

    async def cmd_recall(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        query = " ".join(context.args) if context.args else ""
        session_id = self._session_id_from_update(update)
        self._log_user_message(session_id, f"/recall {query}".strip())
        if not query:
            await self._reply_logged(
                update,
                "Usage: /recall &lt;search query&gt;",
                parse_mode=ParseMode.HTML,
            )
            return

        memories = self.memory.recall(query, top_k=5)
        if not memories:
            await self._reply_logged(update, "🔍 No matching memories found.")
            return

        lines = [f"🔍 <b>Top {len(memories)} memories for:</b> <i>{_escape_html(query)}</i>\n"]
        for i, m in enumerate(memories, 1):
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.timestamp))
            score = f"{m.similarity:.0%}"
            preview = _escape_html(m.content[:100])
            lines.append(f"{i}. [{ts}] ({score}) {m.role}: {preview}")

        await self._reply_logged(update, "\n".join(lines), parse_mode=ParseMode.HTML)

    # ── /skills ───────────────────────────────────────────────

    @staticmethod
    def _skills_usage_text() -> str:
        return (
            "<b>Usage</b>\n"
            "<code>/skills</code> - list installed + active skills\n"
            "<code>/skills search &lt;query&gt;</code> - search ClawHub\n"
            "<code>/skills add &lt;slug|owner/slug|url|slug@version&gt;</code> - install from ClawHub\n"
            "<code>/skills use &lt;id&gt;</code> - activate skill in this chat\n"
            "<code>/skills off &lt;id&gt;</code> - deactivate skill in this chat\n"
            "<code>/skills create &lt;name&gt; [description]</code> - create local skill\n"
            "<code>/skills show &lt;id&gt;</code> - preview SKILL.md\n"
            "<code>/skills remove &lt;id&gt;</code> - uninstall skill"
        )

    def _render_skills_overview(self, session_id: str) -> str:
        installed = self.skills.list_skills()
        active = self.skills.active_records(session_id)
        active_ids = {s.skill_id for s in active}

        lines = ["🧩 <b>Skills</b>", ""]
        if active:
            lines.append(
                "<b>Active in this chat:</b> "
                + ", ".join(f"<code>{_escape_html(s.skill_id)}</code>" for s in active)
            )
        else:
            lines.append("<b>Active in this chat:</b> none")

        lines.append(f"<b>Installed:</b> {len(installed)}")
        lines.append("")

        if installed:
            lines.append("<b>Installed skills</b>")
            for skill in installed:
                marker = "✅ " if skill.skill_id in active_ids else ""
                desc = _escape_html((skill.description or "").strip())
                if len(desc) > 90:
                    desc = desc[:87] + "..."
                version = f" v{_escape_html(skill.version)}" if skill.version else ""
                lines.append(
                    f"• {marker}<code>{_escape_html(skill.skill_id)}</code> "
                    f"({skill.source}{version}) - {_escape_html(skill.name)}"
                )
                if desc:
                    lines.append(f"  {desc}")
        else:
            lines.append("No skills installed yet.")

        lines.append("")
        lines.append(self._skills_usage_text())
        return "\n".join(lines)

    async def cmd_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        session_id = str(update.effective_chat.id) if update.effective_chat else "unknown"
        args = context.args or []
        self._log_user_message(session_id, f"/skills {' '.join(args)}".strip())
        sub = args[0].lower() if args else "list"

        if sub in {"list", "ls"} and len(args) == 1:
            sub = "list"

        if sub == "list":
            text = await asyncio.to_thread(self._render_skills_overview, session_id)
            await self._reply_logged(update, text, parse_mode=ParseMode.HTML)
            return

        if sub in {"search", "find"}:
            query = " ".join(args[1:]).strip() if len(args) > 1 else ""
            if not query:
                await self._reply_logged(
                    update,
                    "Usage: <code>/skills search &lt;query&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            try:
                results = await asyncio.to_thread(self.skills.search_hub, query, 8)
            except SkillError as e:
                await self._reply_logged(
                    update,
                    f"⚠️ Search failed: {_escape_html(str(e))}",
                    parse_mode=ParseMode.HTML,
                )
                return

            if not results:
                await self._reply_logged(
                    update,
                    f"No skills found for <code>{_escape_html(query)}</code>.",
                    parse_mode=ParseMode.HTML,
                )
                return

            lines = [f"🔎 <b>ClawHub results</b> for <code>{_escape_html(query)}</code>", ""]
            for item in results:
                version = f" v{_escape_html(item.version)}" if item.version else ""
                summary = _escape_html(item.summary or "")
                if len(summary) > 110:
                    summary = summary[:107] + "..."
                lines.append(
                    f"• <code>{_escape_html(item.slug)}</code> - {_escape_html(item.display_name)}{version}"
                )
                if summary:
                    lines.append(f"  {summary}")
            lines.append("")
            lines.append("Install: <code>/skills add &lt;slug&gt;</code>")
            await self._reply_logged(update, "\n".join(lines), parse_mode=ParseMode.HTML)
            return

        if sub in {"add", "install", "grab"}:
            if len(args) < 2:
                await self._reply_logged(
                    update,
                    "Usage: <code>/skills add &lt;slug|owner/slug|url|slug@version&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            target = args[1]
            version = args[2].strip() if len(args) > 2 else None
            progress = await self._reply_logged(update, "Installing skill from ClawHub...")

            try:
                skill, replaced = await asyncio.to_thread(
                    self.skills.install_from_hub, target, version
                )
                await asyncio.to_thread(self.skills.activate, session_id, skill.skill_id)
            except SkillError as e:
                fail_text = f"⚠️ Install failed: {_escape_html(str(e))}"
                self._log_bot_message(session_id, self._strip_html_for_log(fail_text))
                await progress.edit_text(
                    fail_text,
                    parse_mode=ParseMode.HTML,
                )
                return

            action = "Updated" if replaced else "Installed"
            lines = [
                f"✅ {action} <code>{_escape_html(skill.skill_id)}</code>",
                f"Name: {_escape_html(skill.name)}",
            ]
            if skill.version:
                lines.append(f"Version: <code>{_escape_html(skill.version)}</code>")
            lines.extend(
                [
                    "",
                    "Auto-activated for this chat.",
                    "List skills: <code>/skills</code>",
                ]
            )
            success_text = "\n".join(lines)
            self._log_bot_message(session_id, self._strip_html_for_log(success_text))
            await progress.edit_text(success_text, parse_mode=ParseMode.HTML)
            return

        if sub in {"use", "enable", "on"}:
            if len(args) < 2:
                await self._reply_logged(
                    update,
                    "Usage: <code>/skills use &lt;id&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            ref = args[1]
            skill = await asyncio.to_thread(self.skills.resolve_skill, ref)
            if not skill:
                await self._reply_logged(
                    update,
                    f"⚠️ Skill not found: <code>{_escape_html(ref)}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            await asyncio.to_thread(self.skills.activate, session_id, skill.skill_id)
            await self._reply_logged(
                update,
                f"✅ Activated <code>{_escape_html(skill.skill_id)}</code> for this chat.",
                parse_mode=ParseMode.HTML,
            )
            return

        if sub in {"off", "disable", "unuse"}:
            if len(args) < 2:
                await self._reply_logged(
                    update,
                    "Usage: <code>/skills off &lt;id&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            ref = args[1]
            skill = await asyncio.to_thread(self.skills.resolve_skill, ref)
            if not skill:
                await self._reply_logged(
                    update,
                    f"⚠️ Skill not found: <code>{_escape_html(ref)}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            await asyncio.to_thread(self.skills.deactivate, session_id, skill.skill_id)
            await self._reply_logged(
                update,
                f"✅ Deactivated <code>{_escape_html(skill.skill_id)}</code> for this chat.",
                parse_mode=ParseMode.HTML,
            )
            return

        if sub in {"create", "new"}:
            if len(args) < 2:
                await self._reply_logged(
                    update,
                    "Usage: <code>/skills create &lt;name&gt; [description]</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            name = args[1]
            description = " ".join(args[2:]).strip() if len(args) > 2 else ""
            try:
                skill = await asyncio.to_thread(self.skills.create_local_skill, name, description)
                await asyncio.to_thread(self.skills.activate, session_id, skill.skill_id)
            except SkillError as e:
                await self._reply_logged(
                    update,
                    f"⚠️ Create failed: {_escape_html(str(e))}",
                    parse_mode=ParseMode.HTML,
                )
                return

            rel_path = f"skills/local/{skill.directory.name}/SKILL.md"
            await self._reply_logged(
                update,
                "\n".join(
                    [
                        f"✅ Created local skill <code>{_escape_html(skill.skill_id)}</code>",
                        f"File: <code>{_escape_html(rel_path)}</code>",
                        "Auto-activated for this chat.",
                    ]
                ),
                parse_mode=ParseMode.HTML,
            )
            return

        if sub in {"show", "view"}:
            if len(args) < 2:
                await self._reply_logged(
                    update,
                    "Usage: <code>/skills show &lt;id&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            ref = args[1]
            skill = await asyncio.to_thread(self.skills.resolve_skill, ref)
            if not skill:
                await self._reply_logged(
                    update,
                    f"⚠️ Skill not found: <code>{_escape_html(ref)}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            try:
                content = await asyncio.to_thread(
                    skill.skill_path.read_text, "utf-8", "replace"
                )
            except Exception as e:
                await self._reply_logged(
                    update,
                    f"⚠️ Failed to read skill: {_escape_html(str(e))}",
                    parse_mode=ParseMode.HTML,
                )
                return

            max_chars = 2000
            preview = content.strip()
            truncated = False
            if len(preview) > max_chars:
                preview = preview[:max_chars].rstrip()
                truncated = True

            msg = (
                f"🧩 <b>{_escape_html(skill.name)}</b> "
                f"(<code>{_escape_html(skill.skill_id)}</code>)\n"
                f"<pre>{_escape_html(preview)}</pre>"
            )
            if truncated:
                msg += "\n<i>Preview truncated.</i>"
            await self._reply_logged(update, msg, parse_mode=ParseMode.HTML)
            return

        if sub in {"remove", "delete", "rm", "uninstall"}:
            if len(args) < 2:
                await self._reply_logged(
                    update,
                    "Usage: <code>/skills remove &lt;id&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            ref = args[1]
            try:
                removed = await asyncio.to_thread(self.skills.remove_skill, ref)
            except SkillError as e:
                await self._reply_logged(
                    update,
                    f"⚠️ Remove failed: {_escape_html(str(e))}",
                    parse_mode=ParseMode.HTML,
                )
                return

            await self._reply_logged(
                update,
                f"🗑️ Removed <code>{_escape_html(removed.skill_id)}</code>.",
                parse_mode=ParseMode.HTML,
            )
            return

        await self._reply_logged(
            update,
            "Unknown /skills subcommand.\n\n" + self._skills_usage_text(),
            parse_mode=ParseMode.HTML,
        )

    # ── /agent ───────────────────────────────────────────────

    async def cmd_agent(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        session_id = str(update.effective_chat.id) if update.effective_chat else "unknown"
        args = context.args or []
        self._log_user_message(session_id, f"/agent {' '.join(args)}".strip())

        sub = args[0].lower() if args else "status"
        if sub in {"list", "ls"}:
            sub = "status"

        if sub == "status":
            await self._reply_logged(
                update,
                self._render_agent_status(session_id),
                parse_mode=ParseMode.HTML,
            )
            return

        if sub in {"doctor", "diag", "check"}:
            report = await asyncio.to_thread(self._render_agent_doctor_report)
            await self._reply_logged(update, report, parse_mode=ParseMode.HTML)
            return

        if sub in {"use", "set", "on"}:
            if len(args) < 2:
                await self._reply_logged(
                    update,
                    "Usage: <code>/agent use &lt;codex|claude|opencode&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            agent = self._resolve_local_agent_name(args[1])
            if not agent:
                await self._reply_logged(
                    update,
                    "Unknown agent. Use one of: <code>codex</code>, "
                    "<code>claude</code>, <code>opencode</code>.",
                    parse_mode=ParseMode.HTML,
                )
                return

            available = self._available_local_agents()
            if agent not in available:
                installed = ", ".join(sorted(available.keys())) if available else "none"
                await self._reply_logged(
                    update,
                    f"⚠️ <code>{_escape_html(agent)}</code> is not installed.\n"
                    f"Installed: <code>{_escape_html(installed)}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            self._agent_mode_by_session[session_id] = agent
            await self._reply_logged(
                update,
                f"✅ Delegation mode enabled: <code>{_escape_html(agent)}</code>\n"
                "All normal chat messages in this chat will now run through this local agent.\n"
                "Disable with <code>/agent off</code>.",
                parse_mode=ParseMode.HTML,
            )
            return

        if sub in {"off", "disable", "stop"}:
            previous = self._agent_mode_by_session.pop(session_id, None)
            removed = await asyncio.to_thread(
                self.memory.delete_delegation_transcripts,
                session_id,
            )
            if previous:
                extra = (
                    f"\n🧹 Removed {removed} delegation transcript(s) from chat memory context."
                    if removed > 0
                    else ""
                )
                await self._reply_logged(
                    update,
                    f"✅ Delegation disabled (was <code>{_escape_html(previous)}</code>).{extra}",
                    parse_mode=ParseMode.HTML,
                )
            else:
                extra = (
                    f"\n🧹 Removed {removed} old delegation transcript(s) from chat memory context."
                    if removed > 0
                    else ""
                )
                await self._reply_logged(
                    update,
                    "Delegation mode is already disabled for this chat." + extra,
                )
            return

        # One-shot convenience: /agent codex <task...>
        direct_agent = self._resolve_local_agent_name(sub)
        if direct_agent:
            task = " ".join(args[1:]).strip()
            if not task:
                await self._reply_logged(
                    update,
                    f"Usage: <code>/agent {_escape_html(direct_agent)} &lt;task&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return
            progress = await self._reply_logged(
                update,
                f"🤖 Delegating to <code>{_escape_html(direct_agent)}</code>...",
                parse_mode=ParseMode.HTML,
            )
            result_text = await self._run_local_agent_task(session_id, direct_agent, task)
            await self._send_response(progress, update, result_text)
            return

        if sub == "run":
            if len(args) < 2:
                await self._reply_logged(
                    update,
                    "Usage: <code>/agent run &lt;task&gt;</code> or "
                    "<code>/agent run &lt;agent&gt; &lt;task&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            requested_agent = self._resolve_local_agent_name(args[1])
            if requested_agent and len(args) >= 3:
                agent = requested_agent
                task = " ".join(args[2:]).strip()
            else:
                agent = self._agent_mode_by_session.get(session_id)
                task = " ".join(args[1:]).strip()

            if not agent:
                await self._reply_logged(
                    update,
                    "No active local agent for this chat.\n"
                    "Set one first: <code>/agent use codex</code> "
                    "(or claude/opencode).",
                    parse_mode=ParseMode.HTML,
                )
                return
            if not task:
                await self._reply_logged(
                    update,
                    "Task is required.",
                )
                return

            progress = await self._reply_logged(
                update,
                f"🤖 Delegating to <code>{_escape_html(agent)}</code>...",
                parse_mode=ParseMode.HTML,
            )
            result_text = await self._run_local_agent_task(session_id, agent, task)
            await self._send_response(progress, update, result_text)
            return

        await self._reply_logged(
            update,
            "Unknown /agent subcommand.\n\n" + self._agent_usage_text(),
            parse_mode=ParseMode.HTML,
        )

    # ── File Operation Helpers ────────────────────────────────

    def _resolve_workspace_path(self, raw_path: str) -> tuple[Path | None, str | None, str | None]:
        """Resolve a user-provided path inside workspace, blocking traversal."""
        path_text = raw_path.strip().strip("`").strip()
        if not path_text:
            return None, None, "empty path"
        if os.path.isabs(path_text):
            return None, None, "absolute paths are not allowed"

        workspace = Path(self.config.workspace_path).resolve()
        candidate = (workspace / path_text).resolve()
        try:
            rel = candidate.relative_to(workspace)
        except ValueError:
            return None, None, "path is outside workspace/"

        if str(rel) == ".":
            return None, None, "path points to workspace root"

        return candidate, rel.as_posix(), None

    def _workspace_display_path(self) -> str:
        """Human-friendly workspace path for status messages."""
        workspace = Path(self.config.workspace_path).resolve()
        runtime_home = os.getenv("LIGHTCLAW_HOME", "").strip()
        if runtime_home:
            try:
                rel = workspace.relative_to(Path(runtime_home).expanduser().resolve())
                return rel.as_posix()
            except ValueError:
                pass
        return workspace.as_posix()

    @staticmethod
    def _build_unified_diff(before: str, after: str, rel_path: str) -> str:
        """Build a unified diff between old and new content."""
        diff_lines = difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            lineterm="\n",
        )
        return "".join(diff_lines).strip()

    @staticmethod
    def _apply_search_replace_hunks(content: str, edit_body: str) -> tuple[str, str | None]:
        """Apply SEARCH/REPLACE hunks with exact-match + unique-match semantics."""
        hunk_pattern = re.compile(
            r"<<<<<<<\s*SEARCH\r?\n([\s\S]*?)\r?\n=======\r?\n([\s\S]*?)\r?\n>>>>>>>\s*REPLACE",
            re.MULTILINE,
        )
        matches = list(hunk_pattern.finditer(edit_body))
        if not matches:
            return content, "no SEARCH/REPLACE hunks found"

        updated = content
        for idx, match in enumerate(matches, 1):
            old_text = match.group(1)
            new_text = match.group(2)

            if old_text == "":
                return content, f"hunk {idx}: SEARCH block is empty"

            occurrences = updated.count(old_text)
            if occurrences == 0:
                return content, f"hunk {idx}: SEARCH text not found (must match exactly)"
            if occurrences > 1:
                return content, f"hunk {idx}: SEARCH text appears {occurrences} times; add more context"

            updated = updated.replace(old_text, new_text, 1)

        return updated, None

    @staticmethod
    def _diff_line_stats(diff_text: str) -> tuple[int, int]:
        """Return added/deleted line counts from unified diff text."""
        added = 0
        deleted = 0
        for line in diff_text.splitlines():
            if line.startswith("+++ ") or line.startswith("--- "):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                deleted += 1
        return added, deleted

    @staticmethod
    def _compact_response_for_file_ops(text: str) -> str:
        """Compress verbose model prose when files were created/edited."""
        if not text:
            return ""

        compact = re.sub(r"\[File (saved|updated|edited): [^\]]+\]", "", text)
        compact = re.sub(r"\[No changes: [^\]]+\]", "", compact)
        compact = re.sub(r"```[\s\S]*?```", "", compact)
        compact = re.sub(r"\n{3,}", "\n\n", compact).strip()
        if not compact:
            return "Done."

        if len(compact) > 320 or compact.count("\n") > 6:
            # Keep only the first paragraph for speed/readability in Telegram.
            first = compact.split("\n\n", 1)[0].strip()
            if len(first) > 220:
                first = first[:217].rstrip() + "..."
            return first or "Done."

        return compact

    @staticmethod
    def _is_incomplete_html_text(text: str) -> bool:
        """Heuristic detection for likely-truncated HTML documents."""
        lower = (text or "").lower()
        if "<html" not in lower and "<!doctype html" not in lower:
            return False
        if "</html>" not in lower or "</body>" not in lower:
            return True
        if lower.count("<section") > lower.count("</section>") + 2:
            return True
        if lower.count("<div") > lower.count("</div>") + 12:
            return True
        return False

    @staticmethod
    def _render_file_operations(
        operations: list[FileOperationResult],
        include_diffs: bool = True,
        workspace_label: str = "workspace/",
    ) -> str:
        """Render a human-readable summary of applied file operations."""
        if not operations:
            return ""

        success = [op for op in operations if op.action != "error"]
        failures = [op for op in operations if op.action == "error"]
        lines: list[str] = []
        total_added = 0
        total_deleted = 0

        if success:
            lines.append(f"✅ Applied {len(success)} file operation(s):")
            for op in success:
                change_hint = ""
                if op.diff:
                    added, deleted = LightClawBot._diff_line_stats(op.diff)
                    total_added += added
                    total_deleted += deleted
                    if include_diffs:
                        change_hint = f" (+{added}/-{deleted} lines)"

                if op.action in ("created", "auto_created"):
                    lines.append(f"- Created `{op.path}`{change_hint}")
                elif op.action == "updated":
                    lines.append(f"- Updated `{op.path}`{change_hint}")
                elif op.action == "edited":
                    lines.append(f"- Edited `{op.path}`{change_hint}")
                elif op.action == "unchanged":
                    lines.append(f"- No changes in `{op.path}`")
                else:
                    lines.append(f"- `{op.path}`")

            if include_diffs and (total_added or total_deleted):
                lines.append(f"- Diff summary: +{total_added} / -{total_deleted} lines")

            lines.append("")
            lines.append(f"📁 Saved to {workspace_label}")

        if failures:
            if lines:
                lines.append("")
            lines.append(f"⚠️ {len(failures)} file operation(s) failed:")
            for op in failures:
                detail = op.detail or "unknown error"
                lines.append(f"- `{op.path}`: {detail}")

        return "\n".join(lines).strip()

    # ── File Operation Tool ───────────────────────────────────

    async def _process_file_blocks(self, response: str) -> tuple[list[FileOperationResult], str]:
        """Apply edit/create instructions from model response and return cleaned text."""
        operations: list[FileOperationResult] = []
        cleaned_response = response

        lang_extensions = {
            "html": ".html",
            "htm": ".html",
            "css": ".css",
            "javascript": ".js",
            "js": ".js",
            "python": ".py",
            "py": ".py",
            "json": ".json",
            "xml": ".xml",
            "sql": ".sql",
            "markdown": ".md",
            "md": ".md",
            "bash": ".sh",
            "sh": ".sh",
            "txt": ".txt",
            "java": ".java",
            "ts": ".ts",
            "tsx": ".tsx",
            "jsx": ".jsx",
            "go": ".go",
            "rs": ".rs",
            "c": ".c",
            "cpp": ".cpp",
            "yaml": ".yaml",
            "yml": ".yml",
        }

        edit_pattern = re.compile(
            r"```edit:(?P<path>[^\n`]+)\s*\n(?P<body>[\s\S]*?)```",
            re.IGNORECASE,
        )

        def success_count() -> int:
            return sum(1 for op in operations if op.action != "error")

        def write_workspace_file(raw_path: str, content: str, auto_generated: bool = False) -> str:
            target, rel_path, path_err = self._resolve_workspace_path(raw_path)
            display_path = rel_path or raw_path.strip() or "unknown"
            if path_err or target is None or rel_path is None:
                operations.append(FileOperationResult("error", display_path, path_err or "invalid path"))
                return f"[Save failed: {display_path}]"

            before = None
            if target.exists():
                try:
                    before = target.read_text(encoding="utf-8")
                except Exception as e:
                    operations.append(FileOperationResult("error", rel_path, f"failed to read file: {e}"))
                    return f"[Save failed: {rel_path}]"

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            except Exception as e:
                operations.append(FileOperationResult("error", rel_path, f"failed to write file: {e}"))
                return f"[Save failed: {rel_path}]"

            if before is None:
                action = "auto_created" if auto_generated else "created"
                diff_text = self._build_unified_diff("", content, rel_path)
                operations.append(FileOperationResult(action, rel_path, diff=diff_text))
                log.info(f"Saved file: {target}")
                return f"[File saved: {rel_path}]"

            if before == content:
                operations.append(FileOperationResult("unchanged", rel_path))
                return f"[No changes: {rel_path}]"

            diff_text = self._build_unified_diff(before, content, rel_path)
            operations.append(FileOperationResult("updated", rel_path, diff=diff_text))
            log.info(f"Updated file: {target}")
            return f"[File updated: {rel_path}]"

        def _max_overlap_suffix_prefix(left: str, right: str, max_len: int = 1500) -> int:
            if not left or not right:
                return 0
            max_check = min(len(left), len(right), max_len)
            for size in range(max_check, 0, -1):
                if left.endswith(right[:size]):
                    return size
            return 0

        def _strip_outer_code_fence(text: str) -> tuple[str, bool]:
            """Return (content_without_wrapping_fence, saw_closing_fence)."""
            chunk = (text or "").strip()
            if not chunk:
                return "", False

            wrapped = re.match(r"^```[^\n`]*\n([\s\S]*?)\n```$", chunk)
            if wrapped:
                return wrapped.group(1).strip(), True

            if chunk.startswith("```"):
                nl = chunk.find("\n")
                if nl >= 0:
                    chunk = chunk[nl + 1 :]

            if chunk.strip() == "```":
                return "", True

            close_idx = chunk.find("\n```")
            if close_idx >= 0:
                return chunk[:close_idx].rstrip(), True

            if chunk.endswith("```"):
                return chunk[:-3].rstrip(), True

            return chunk.strip(), False

        async def complete_unclosed_named_fence(
            lang: str,
            raw_path: str,
            partial_content: str,
        ) -> tuple[str, bool]:
            """Try to continue a truncated ```lang:path fenced block."""
            assembled = (partial_content or "").rstrip()
            lang_name = (lang or "txt").strip().lower()
            attempts = 3

            for _ in range(attempts):
                if lang_name in {"html", "htm"} and not self._is_incomplete_html_text(assembled):
                    return assembled, True

                continuation_system = (
                    "You are continuing a truncated fenced file block.\n"
                    "Return ONLY the missing tail starting from the exact next character.\n"
                    "Do NOT repeat already-sent text.\n"
                    "Do NOT include explanations.\n"
                    "When complete, end with a closing fence line: ```"
                )
                continuation_user = (
                    f"The following block was truncated before the closing fence.\n\n"
                    f"```{lang_name}:{raw_path}\n"
                    f"{assembled}\n\n"
                    "Continue now from the next character only."
                )

                try:
                    continuation = await self.llm.chat(
                        [{"role": "user", "content": continuation_user}],
                        system_prompt=continuation_system,
                    )
                except Exception as e:
                    log.error(f"Continuation pass failed for {raw_path}: {e}")
                    return assembled, False

                if not continuation:
                    break

                # If model returned a full fenced replacement block, use it directly.
                full_block = re.search(
                    rf"```[a-zA-Z0-9_+\-]*:{re.escape(raw_path)}\s*\n([\s\S]*?)```",
                    continuation,
                    re.IGNORECASE,
                )
                if full_block:
                    candidate = full_block.group(1).strip()
                    return candidate, True

                piece, saw_closing = _strip_outer_code_fence(continuation)
                if piece:
                    overlap = _max_overlap_suffix_prefix(assembled, piece)
                    piece = piece[overlap:]
                    if piece:
                        if assembled and not assembled.endswith("\n") and not piece.startswith("\n"):
                            assembled += "\n"
                        assembled += piece

                if saw_closing:
                    if lang_name in {"html", "htm"} and self._is_incomplete_html_text(assembled):
                        continue
                    return assembled, True

            if lang_name in {"html", "htm"} and not self._is_incomplete_html_text(assembled):
                return assembled, True

            return assembled, False

        async def complete_unclosed_generic_fence(
            lang: str,
            partial_content: str,
        ) -> tuple[str, bool]:
            """Try to continue a truncated ```lang fenced block with no explicit path."""
            assembled = (partial_content or "").rstrip()
            lang_name = (lang or "txt").strip().lower()

            for _ in range(2):
                continuation_system = (
                    "You are continuing a truncated fenced code block.\n"
                    "Return ONLY the missing tail from the exact next character.\n"
                    "No explanation. End with ``` when complete."
                )
                continuation_user = (
                    f"```{lang_name}\n"
                    f"{assembled}\n\n"
                    "Continue now from the next character only."
                )
                try:
                    continuation = await self.llm.chat(
                        [{"role": "user", "content": continuation_user}],
                        system_prompt=continuation_system,
                    )
                except Exception:
                    return assembled, False

                if not continuation:
                    break

                piece, saw_closing = _strip_outer_code_fence(continuation)
                if piece:
                    overlap = _max_overlap_suffix_prefix(assembled, piece)
                    piece = piece[overlap:]
                    if piece:
                        if assembled and not assembled.endswith("\n") and not piece.startswith("\n"):
                            assembled += "\n"
                        assembled += piece
                if saw_closing:
                    return assembled, True

            return assembled, False

        def apply_edit_block(match: re.Match) -> str:
            raw_path = match.group("path").strip()
            edit_body = match.group("body").strip("\n")

            target, rel_path, path_err = self._resolve_workspace_path(raw_path)
            display_path = rel_path or raw_path or "unknown"
            if path_err or target is None or rel_path is None:
                operations.append(FileOperationResult("error", display_path, path_err or "invalid path"))
                return f"[Edit failed: {display_path}]"

            if not target.exists():
                operations.append(FileOperationResult("error", rel_path, "file not found"))
                return f"[Edit failed: {rel_path}]"

            try:
                before = target.read_text(encoding="utf-8")
            except Exception as e:
                operations.append(FileOperationResult("error", rel_path, f"failed to read file: {e}"))
                return f"[Edit failed: {rel_path}]"

            after, apply_err = self._apply_search_replace_hunks(before, edit_body)
            if apply_err:
                operations.append(FileOperationResult("error", rel_path, apply_err))
                return f"[Edit failed: {rel_path}]"

            if after == before:
                operations.append(FileOperationResult("unchanged", rel_path))
                return f"[No changes: {rel_path}]"

            try:
                target.write_text(after, encoding="utf-8")
            except Exception as e:
                operations.append(FileOperationResult("error", rel_path, f"failed to write file: {e}"))
                return f"[Edit failed: {rel_path}]"

            diff_text = self._build_unified_diff(before, after, rel_path)
            operations.append(FileOperationResult("edited", rel_path, diff=diff_text))
            log.info(f"Applied edit block: {target}")
            return f"[File edited: {rel_path}]"

        cleaned_response = re.sub(edit_pattern, apply_edit_block, cleaned_response)

        pattern_named = re.compile(
            r"```([a-zA-Z0-9_+\-]+):([^\n`]+)\s*\n([\s\S]*?)```",
            re.MULTILINE,
        )

        def apply_named_file_block(match: re.Match) -> str:
            raw_path = match.group(2).strip()
            content = match.group(3).strip()
            return write_workspace_file(raw_path, content)

        cleaned_response = re.sub(pattern_named, apply_named_file_block, cleaned_response)

        # Common malformed style: ```index.html ... ```
        pattern_filename_fence = re.compile(
            r"```(?P<path>[^\n`]+\.[a-zA-Z0-9]{1,10})\s*\n(?P<body>[\s\S]*?)```",
            re.MULTILINE,
        )

        def apply_filename_fence_block(match: re.Match) -> str:
            raw_path = match.group("path").strip()
            content = match.group("body").strip()
            return write_workspace_file(raw_path, content)

        cleaned_response = re.sub(pattern_filename_fence, apply_filename_fence_block, cleaned_response)

        pattern_file_label = re.compile(
            r"File:\s*([^\n`]+)\s*\n```([a-zA-Z0-9_+\-]+)?\s*\n?([\s\S]*?)```",
            re.IGNORECASE,
        )

        def apply_file_label_block(match: re.Match) -> str:
            raw_path = match.group(1).strip()
            content = match.group(3).strip()
            return write_workspace_file(raw_path, content)

        cleaned_response = re.sub(pattern_file_label, apply_file_label_block, cleaned_response)

        file_counter = 1
        pattern_auto = re.compile(r"```([a-zA-Z0-9_+\-]+)?\s*\n([\s\S]*?)```")

        def is_code_like(lang: str, content: str) -> bool:
            if lang and lang not in {"text", "txt", "plain"}:
                return True
            hints = ("<!doctype", "<html", "{", "};", "function ", "class ", "import ", "def ")
            lowered = content.lower()
            return any(h in lowered for h in hints)

        def apply_auto_block(match: re.Match) -> str:
            nonlocal file_counter
            lang = (match.group(1) or "txt").strip().lower()
            content = match.group(2).strip()

            # Keep tiny snippets inline; move large/code-like blocks to workspace.
            if lang == "diff":
                return match.group(0)
            if len(content) < 120 and not is_code_like(lang, content):
                return match.group(0)

            ext = lang_extensions.get(lang, ".txt")
            filename = f"output_{int(time.time())}_{file_counter}{ext}"
            file_counter += 1
            return write_workspace_file(filename, content, auto_generated=True)

        cleaned_response = re.sub(pattern_auto, apply_auto_block, cleaned_response)

        # Salvage malformed/unclosed named fence: ```html:index.html ...EOF
        if success_count() == 0:
            unclosed_named = re.search(
                r"```([a-zA-Z0-9_+\-]+):([^\n`]+)\s*\n([\s\S]+)$",
                cleaned_response,
                re.MULTILINE,
            )
            if unclosed_named:
                lang = (unclosed_named.group(1) or "txt").strip().lower()
                raw_path = unclosed_named.group(2).strip()
                content = unclosed_named.group(3).strip()
                if content:
                    completed_content, completed = await complete_unclosed_named_fence(
                        lang=lang,
                        raw_path=raw_path,
                        partial_content=content,
                    )
                    if completed:
                        marker = write_workspace_file(raw_path, completed_content)
                    else:
                        _, rel_path, _ = self._resolve_workspace_path(raw_path)
                        display_path = rel_path or raw_path or "unknown"
                        operations.append(
                            FileOperationResult(
                                "error",
                                display_path,
                                "incomplete code block (model output truncated before closing fence)",
                            )
                        )
                        marker = f"[Save failed: {display_path}]"
                    prefix = cleaned_response[: unclosed_named.start()].rstrip()
                    cleaned_response = (
                        (prefix + "\n\n" if prefix else "")
                        + marker
                    ).strip()

        # Salvage malformed/unclosed generic fence: ```html ...EOF
        if success_count() == 0:
            unclosed_generic = re.search(
                r"```([a-zA-Z0-9_+\-]+)?\s*\n([\s\S]+)$",
                cleaned_response,
                re.MULTILINE,
            )
            if unclosed_generic:
                lang = (unclosed_generic.group(1) or "txt").strip().lower()
                content = unclosed_generic.group(2).strip()
                if lang != "diff" and len(content) >= 120:
                    ext = lang_extensions.get(lang, ".txt")
                    filename = f"output_{int(time.time())}_unclosed{ext}"
                    completed_content, completed = await complete_unclosed_generic_fence(
                        lang=lang,
                        partial_content=content,
                    )
                    if completed:
                        marker = write_workspace_file(filename, completed_content, auto_generated=True)
                    else:
                        operations.append(
                            FileOperationResult(
                                "error",
                                filename,
                                "incomplete code block (model output truncated before closing fence)",
                            )
                        )
                        marker = f"[Save failed: {filename}]"
                    prefix = cleaned_response[: unclosed_generic.start()].rstrip()
                    cleaned_response = (
                        (prefix + "\n\n" if prefix else "")
                        + marker
                    ).strip()

        # Last resort: HTML document without fences.
        if success_count() == 0:
            html_start = cleaned_response.lower().find("<!doctype html")
            if html_start < 0:
                html_start = cleaned_response.lower().find("<html")
            if html_start >= 0:
                html = cleaned_response[html_start:].strip()
                if len(html) >= 200:
                    if self._is_incomplete_html_text(html):
                        operations.append(
                            FileOperationResult(
                                "error",
                                "index.html",
                                "incomplete html output (missing closing tags)",
                            )
                        )
                        marker = "[Save failed: index.html]"
                    else:
                        marker = write_workspace_file("index.html", html, auto_generated=True)
                    intro = cleaned_response[:html_start].strip()
                    cleaned_response = (f"{intro}\n\n{marker}" if intro else marker).strip()

        # Never return huge fenced code in chat: remove any remaining large code blocks.
        def strip_large_leftover_code(match: re.Match) -> str:
            content = match.group(2).strip()
            if len(content) >= 120:
                return "[Large code omitted in chat]"
            return match.group(0)

        cleaned_response = re.sub(pattern_auto, strip_large_leftover_code, cleaned_response)

        return operations, cleaned_response.strip()

    async def _retry_failed_edits(
        self,
        user_text: str,
        original_model_response: str,
        failed_ops: list[FileOperationResult],
    ) -> tuple[list[FileOperationResult], str]:
        """Retry failed edit operations once using exact current file content."""
        retryable_errors = [
            op for op in failed_ops
            if op.action == "error"
            and any(
                marker in op.detail
                for marker in (
                    "SEARCH text not found",
                    "SEARCH text appears",
                    "no SEARCH/REPLACE hunks found",
                )
            )
        ]
        if not retryable_errors:
            return [], ""

        snippets: list[str] = []
        retry_paths: list[str] = []
        for op in retryable_errors:
            target, rel_path, path_err = self._resolve_workspace_path(op.path)
            if path_err or target is None or rel_path is None:
                continue
            if not target.exists():
                continue
            try:
                content = target.read_text(encoding="utf-8")
            except Exception:
                continue

            # Keep retry prompt bounded.
            max_chars = 9000
            shown = content[:max_chars]
            if len(content) > max_chars:
                shown += "\n... [truncated]"

            snippets.append(f"### {rel_path}\n```text\n{shown}\n```")
            retry_paths.append(rel_path)

        if not snippets:
            return [], ""

        retry_system = (
            "You are a precise code editor. "
            "Return ONLY edit blocks in this exact format:\n"
            "```edit:path/to/file.ext\n"
            "<<<<<<< SEARCH\n"
            "exact old text\n"
            "=======\n"
            "new text\n"
            ">>>>>>> REPLACE\n"
            "```\n"
            "Do not include prose."
        )
        retry_user = (
            "The previous edit failed because SEARCH text did not match exactly.\n\n"
            f"Original user request:\n{user_text}\n\n"
            "Previous model response:\n"
            f"{original_model_response}\n\n"
            "Current file contents:\n"
            f"{'\n\n'.join(snippets)}\n\n"
            "Generate corrected edit blocks that apply exactly to these files. "
            "If no change is needed, reply exactly: NO_CHANGES"
        )

        try:
            retry_response = await self.llm.chat(
                [{"role": "user", "content": retry_user}],
                system_prompt=retry_system,
            )
        except Exception as e:
            log.error(f"Retry edit call failed: {e}")
            return [], ""

        if not retry_response or retry_response.strip().upper() == "NO_CHANGES":
            return [], ""

        retry_ops, retry_cleaned = await self._process_file_blocks(retry_response)

        if retry_ops:
            ok_count = sum(1 for op in retry_ops if op.action != "error")
            err_count = sum(1 for op in retry_ops if op.action == "error")
            log.info(
                f"Retry edit result for {', '.join(retry_paths)}: "
                f"{ok_count} succeeded, {err_count} failed"
            )

        return retry_ops, retry_cleaned

    async def _force_file_ops_pass(
        self,
        session_id: str,
        user_text: str,
        prior_model_response: str,
    ) -> tuple[list[FileOperationResult], str]:
        """Force a file operation pass when the model returned prose/no-op."""
        target_files = self._collect_workspace_candidates(user_text, session_id, limit=4)
        snippets: list[str] = []
        for rel_path in target_files:
            target, _, err = self._resolve_workspace_path(rel_path)
            if err or target is None or not target.exists():
                continue
            try:
                content = target.read_text(encoding="utf-8")
            except Exception:
                continue
            shown = content[:14000]
            if len(content) > 14000:
                shown += "\n... [truncated]"
            snippets.append(f"### {rel_path}\n```text\n{shown}\n```")

        file_context = (
            f"Current candidate workspace files:\n{'\n\n'.join(snippets)}"
            if snippets
            else "Current candidate workspace files:\n(none yet - create new files in workspace as needed)"
        )

        forced_system = (
            "You are a file operation engine for LightClaw. "
            "Do NOT ask to inspect/read files. You already have file contents. "
            "You MUST perform the requested modifications now.\n"
            "Return ONLY file operation blocks:\n"
            "1) Edits:\n"
            "```edit:path/to/file.ext\n"
            "<<<<<<< SEARCH\n"
            "exact old text\n"
            "=======\n"
            "new text\n"
            ">>>>>>> REPLACE\n"
            "```\n"
            "2) Full rewrite if large changes:\n"
            "```lang:path/to/file.ext\n"
            "<full file>\n"
            "```\n"
            "Use a language tag that matches the file extension.\n"
            "No prose."
        )
        forced_user = (
            f"User request:\n{user_text}\n\n"
            "Previous model response (incorrect/no-op):\n"
            f"{prior_model_response}\n\n"
            f"{file_context}\n\n"
            "Now apply the request directly. Return file operation blocks only."
        )

        try:
            forced_response = await self.llm.chat(
                [{"role": "user", "content": forced_user}],
                system_prompt=forced_system,
            )
        except Exception as e:
            log.error(f"Forced file-op pass failed: {e}")
            return [], ""

        if not forced_response:
            return [], ""

        return await self._process_file_blocks(forced_response)

    async def _repair_incomplete_html(
        self,
        session_id: str,
        user_text: str,
        file_ops: list[FileOperationResult],
    ) -> list[FileOperationResult]:
        """Repair likely-truncated HTML files created/updated by the model."""
        repair_ops: list[FileOperationResult] = []
        success_ops = [op for op in file_ops if op.action != "error"]
        html_paths = [op.path for op in success_ops if op.path.lower().endswith((".html", ".htm"))]
        if not html_paths:
            return repair_ops

        # Preserve order while avoiding duplicate repair attempts per path.
        seen_paths: set[str] = set()
        ordered_html_paths: list[str] = []
        for path in html_paths:
            if path in seen_paths:
                continue
            seen_paths.add(path)
            ordered_html_paths.append(path)

        for rel_path in ordered_html_paths:
            target, _, err = self._resolve_workspace_path(rel_path)
            if err or target is None or not target.exists():
                continue

            max_attempts = 3
            repaired = False

            for attempt in range(1, max_attempts + 1):
                try:
                    content = target.read_text(encoding="utf-8")
                except Exception:
                    break

                if not self._is_incomplete_html_text(content):
                    repaired = True
                    break

                repair_system = (
                    "You are an HTML repair engine. "
                    "The file below is truncated/incomplete. "
                    "Return ONLY one full-file block in this format:\n"
                    "```html:path/to/file.html\n"
                    "<complete valid HTML document>\n"
                    "```\n"
                    "CRITICAL:\n"
                    "- Include </body> and </html>\n"
                    "- Return full file, not a diff\n"
                    "- No prose."
                )
                repair_user = (
                    f"Attempt: {attempt}/{max_attempts}\n"
                    f"User context/request:\n{user_text}\n\n"
                    f"Repair this file and keep its design intent:\n"
                    f"Path: {rel_path}\n"
                    "Current content:\n"
                    f"```html\n{content}\n```"
                )
                try:
                    repair_response = await self.llm.chat(
                        [{"role": "user", "content": repair_user}],
                        system_prompt=repair_system,
                    )
                except Exception as e:
                    log.error(f"HTML repair pass failed for {rel_path}: {e}")
                    continue

                if not repair_response:
                    continue

                ops, _ = await self._process_file_blocks(repair_response)
                if ops:
                    ok = sum(1 for op in ops if op.action != "error")
                    err_count = sum(1 for op in ops if op.action == "error")
                    log.info(
                        f"[{session_id}] HTML repair {rel_path} (attempt {attempt}): "
                        f"{ok} succeeded, {err_count} failed"
                    )
                    repair_ops.extend(ops)

                try:
                    updated = target.read_text(encoding="utf-8")
                except Exception:
                    updated = ""
                if updated and not self._is_incomplete_html_text(updated):
                    repaired = True
                    break

            if not repaired:
                repair_ops.append(
                    FileOperationResult(
                        "error",
                        rel_path,
                        "html file still incomplete after repair attempts",
                    )
                )

        return repair_ops

    # ── /show ─────────────────────────────────────────────────

    async def cmd_show(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        session_id = str(update.effective_chat.id) if update.effective_chat else "?"
        self._log_user_message(session_id, "/show")

        uptime = int(time.time() - self.start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)

        stats = self.memory.stats()
        summary_status = "✅" if session_id in self._session_summaries else "—"
        active_skills = self.skills.active_records(session_id)
        installed_skills = self.skills.list_skills()
        active_agent = self._agent_mode_by_session.get(session_id, "none")

        voice_status = "✅ Groq Whisper" if self.config.groq_api_key else "❌ No GROQ_API_KEY"

        await self._reply_logged(
            update,
            f"🦞 <b>LightClaw Status</b>\n\n"
            f"<b>Provider:</b> {_escape_html(self.config.llm_provider)}\n"
            f"<b>Model:</b> {_escape_html(self.config.llm_model)}\n"
            f"<b>Context window:</b> {self.config.context_window:,} tokens\n"
            f"<b>Max output:</b> {self.config.max_output_tokens:,} tokens\n"
            f"<b>Uptime:</b> {hours}h {minutes}m {seconds}s\n"
            f"<b>Memory:</b> {stats['total_interactions']} interactions\n"
            f"<b>Session summary:</b> {summary_status}\n"
            f"<b>Skills:</b> {len(active_skills)} active / {len(installed_skills)} installed\n"
            f"<b>Delegation:</b> {_escape_html(active_agent)}\n"
            f"<b>Voice:</b> {voice_status}",
            parse_mode=ParseMode.HTML,
        )

    # ── Voice Message Handler ─────────────────────────────────

    async def handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle voice messages — download, transcribe, then process as text."""
        if not update.effective_user or not update.message or not update.message.voice:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        voice = update.message.voice
        chat_id = update.effective_chat.id if update.effective_chat else 0

        # Send typing indicator immediately
        if update.effective_chat:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        # Download voice file
        try:
            voice_file = await voice.get_file()
            voice_bytes = await voice_file.download_as_bytearray()
        except Exception as e:
            log.error(f"Failed to download voice: {e}")
            await self._reply_logged(update, "⚠️ Couldn't download voice message.")
            return

        # Transcribe
        text = await transcribe_voice(bytes(voice_bytes), self.config.groq_api_key)

        if text:
            caption = update.message.caption or ""
            user_text = f"[voice transcription: {text}]"
            if caption:
                user_text = f"{caption}\n{user_text}"
            log.info(f"Voice transcribed: {text[:80]}")
        else:
            user_text = "[voice message received — transcription not available]"
            if update.message.caption:
                user_text = f"{update.message.caption}\n{user_text}"

        # Process through the normal agent loop
        await self._process_user_message(update, context, user_text)

    # ── Photo Handler ─────────────────────────────────────────

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle photo messages — note the image and process caption."""
        if not update.effective_user or not update.message or not update.message.photo:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        caption = update.message.caption or ""
        user_text = f"[image: photo attached]\n{caption}" if caption else "[image: photo attached]"

        await self._process_user_message(update, context, user_text)

    # ── Document Handler ──────────────────────────────────────

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle document messages."""
        if not update.effective_user or not update.message or not update.message.document:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        doc = update.message.document
        filename = doc.file_name or "unknown file"
        caption = update.message.caption or ""
        user_text = f"[document: {filename}]\n{caption}" if caption else f"[document: {filename}]"

        await self._process_user_message(update, context, user_text)

    # ── Message Handler (the core loop) ───────────────────────

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages — the main conversational agent loop."""
        if not update.effective_user or not update.message or not update.message.text:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        await self._process_user_message(update, context, update.message.text)

    # ── Core Processing Pipeline ──────────────────────────────

    async def _process_user_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str
    ):
        """
        Core agent loop:
        1. Send "Thinking… 💭" placeholder
        2. Recall relevant memories (RAG)
        3. Get recent conversation history + clean orphans
        4. Build system prompt with personality + memories + summary
        5. Send to LLM (with retry on context overflow)
        6. Ingest user message into memory
        7. Apply file create/edit operations from model response
        8. Ingest cleaned assistant response into memory
        9. Edit placeholder with final response
        10. Trigger async summarization if needed
        """
        chat_id = update.effective_chat.id if update.effective_chat else 0
        session_id = str(chat_id)

        self._log_user_message(session_id, user_text)

        # 1. Send typing + placeholder
        placeholder = None
        try:
            if update.effective_chat:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            self._log_bot_message(session_id, "Thinking... 💭")
            placeholder = await update.message.reply_text("Thinking... 💭")
        except Exception:
            pass

        # Optional delegation mode: route normal messages to local coding agent.
        active_agent = self._agent_mode_by_session.get(session_id)
        if active_agent:
            log.info(
                f"[{session_id}] Delegation mode active ({active_agent}); routing message to local agent"
            )
            self.memory.ingest("user", user_text, session_id)
            delegated_response = await self._run_local_agent_task(
                session_id=session_id,
                agent=active_agent,
                task=user_text,
            )
            self.memory.ingest("assistant", delegated_response, session_id)
            await self._send_response(placeholder, update, delegated_response)
            if not self._llm_backoff_active():
                asyncio.create_task(self.maybe_summarize(session_id))
            return

        # Provider backoff: avoid hammering the API on every user message.
        if self._llm_backoff_active():
            remaining = self._llm_backoff_remaining_sec()
            wait_hint = f"{remaining}s" if remaining > 0 else "a short while"
            self.memory.ingest("user", user_text, session_id)
            quick_reply = (
                f"⚠️ {self.config.llm_provider} is temporarily unavailable "
                "(quota/billing or rate limit).\n"
                f"Please retry in about {wait_hint}, or top up your provider balance."
            )
            await self._send_response(placeholder, update, quick_reply)
            return

        # 2. Recall relevant memories
        memories = self.memory.recall(user_text, top_k=self.config.memory_top_k)
        memories = self._filter_recalled_memories(memories)
        memories_text = self.memory.format_memories_for_prompt(memories)

        # 3. Get recent conversation history + clean orphans
        recent = self.memory.get_recent(session_id, limit=20)
        recent = self._clean_orphan_messages(recent)
        recent = self._filter_recent_context(recent)

        # 4. Get session summary
        summary = self._get_session_summary(session_id)
        skills_text = await asyncio.to_thread(self.skills.prompt_context, session_id)

        # 5. Build system prompt with personality
        system_prompt = build_system_prompt(
            self.config, self.personality, memories_text, summary, skills_text
        )

        # 6. Build messages for LLM
        messages = list(recent)
        messages.append({"role": "user", "content": user_text})

        # 7. Call LLM (with retry on context overflow)
        start_time_mono = time.monotonic()
        response = None
        max_retries = 2

        for retry in range(max_retries + 1):
            try:
                response = await self.llm.chat(messages, system_prompt)
                break
            except Exception as e:
                if retry < max_retries and self._is_context_error(str(e)):
                    log.warning(f"Context overflow detected, compressing history (retry {retry + 1})")
                    # Emergency compression: drop oldest 50%
                    if len(messages) > 4:
                        mid = len(messages) // 2
                        messages = (
                            messages[:1]
                            + [{"role": "system", "content": f"[Emergency: dropped {mid} oldest messages due to context limit]"}]
                            + messages[mid:]
                        )
                    continue
                log.error(f"LLM call failed: {e}")
                response = f"⚠️ Error communicating with {self.config.llm_provider}: {e}"
                break

        if response is None:
            response = "⚠️ Failed to get a response after retries. Please try again."
        provider_error_response = self._is_provider_error_text(response)
        if provider_error_response:
            self._set_llm_backoff()
        else:
            self._clear_llm_backoff()

        elapsed = time.monotonic() - start_time_mono
        log.info(f"[{session_id}] LLM response ({elapsed:.1f}s)")

        # 8. Ingest into memory
        self.memory.ingest("user", user_text, session_id)

        # 9. Apply file operations (create/edit) and clean the response
        file_ops, cleaned_response = await self._process_file_blocks(response)
        failed_ops = [op for op in file_ops if op.action == "error"]
        if failed_ops:
            retry_ops, retry_cleaned = await self._retry_failed_edits(
                user_text=user_text,
                original_model_response=response,
                failed_ops=failed_ops,
            )
            if retry_ops:
                recovered_paths = {op.path for op in retry_ops if op.action != "error"}
                if recovered_paths:
                    file_ops = [
                        op for op in file_ops
                        if not (op.action == "error" and op.path in recovered_paths)
                    ]
                    if retry_cleaned:
                        cleaned_response = "\n\n".join(
                            part for part in [cleaned_response, retry_cleaned] if part
                        ).strip()
                file_ops.extend(retry_ops)

        # 9b. Force a second pass when the model returned no-op prose for file tasks.
        success_ops = [op for op in file_ops if op.action != "error"]
        if not success_ops and (
            self._is_file_intent(user_text)
            or self._is_deferral_response(response)
            or self._is_deferral_response(cleaned_response)
        ):
            forced_ops, forced_cleaned = await self._force_file_ops_pass(
                session_id=session_id,
                user_text=user_text,
                prior_model_response=response,
            )
            if forced_ops:
                recovered_paths = {op.path for op in forced_ops if op.action != "error"}
                if recovered_paths:
                    file_ops = [op for op in file_ops if op.path not in recovered_paths]
                file_ops.extend(forced_ops)
                if forced_cleaned:
                    cleaned_response = "\n\n".join(
                        part for part in [cleaned_response, forced_cleaned] if part
                    ).strip()

        # 9c. Repair likely-truncated HTML outputs before user-facing response.
        repair_ops = await self._repair_incomplete_html(session_id, user_text, file_ops)
        if repair_ops:
            repaired_paths = {op.path for op in repair_ops if op.action != "error"}
            if repaired_paths:
                file_ops = [op for op in file_ops if op.path not in repaired_paths]
            file_ops.extend(repair_ops)

        # Track last touched file to support follow-up edit requests like "add more".
        success_ops = [op for op in file_ops if op.action != "error" and op.path]
        if success_ops:
            self._last_file_by_session[session_id] = success_ops[-1].path

        # 10. Build final message (short text + file operation summary)
        workspace_label = self._workspace_display_path()
        visible_response = cleaned_response
        if file_ops:
            success_count = sum(1 for op in file_ops if op.action != "error")
            if success_count > 0:
                visible_response = "Done. Saved requested changes to files."
            else:
                visible_response = self._compact_response_for_file_ops(cleaned_response)

        response_parts = [visible_response] if visible_response else []
        if file_ops:
            response_parts.append(
                self._render_file_operations(
                    file_ops,
                    include_diffs=True,
                    workspace_label=workspace_label,
                )
            )
        final_markdown_response = "\n\n".join(part for part in response_parts if part).strip()
        if self._is_large_code_leak(final_markdown_response):
            # Hard guardrail: never send giant code dumps to Telegram.
            if file_ops:
                final_markdown_response = (
                    "Done. Saved requested changes to files.\n\n"
                    + self._render_file_operations(
                        file_ops,
                        include_diffs=True,
                        workspace_label=workspace_label,
                    )
                )
            else:
                final_markdown_response = (
                    "Large code output was suppressed.\n"
                    "Please ask again and include explicit file names (e.g. ```html:index.html ...```)."
                )
        if not final_markdown_response:
            final_markdown_response = "Done."

        # Ingest a compact version into memory (without long diff blocks)
        memory_text = visible_response if file_ops else cleaned_response
        memory_parts = [memory_text] if memory_text else []
        if file_ops:
            memory_parts.append(
                self._render_file_operations(
                    file_ops,
                    include_diffs=False,
                    workspace_label=workspace_label,
                )
            )
        memory_response = "\n\n".join(part for part in memory_parts if part).strip() or "Done."
        self.memory.ingest("assistant", memory_response, session_id)

        # 11. Edit placeholder with final response
        await self._send_response(placeholder, update, final_markdown_response)

        # 12. Async summarization check
        if not provider_error_response:
            asyncio.create_task(self.maybe_summarize(session_id))

    # ── Message Chunking (Telegram 4096 char limit) ─────────

    @staticmethod
    def _chunk_message(text: str, max_len: int = 3500) -> list[str]:
        """Split a long message into chunks that fit Telegram's limit.

        Splits at newline boundaries to avoid breaking HTML tags or words.
        Uses 3500 instead of 4096 to leave room for HTML entity expansion
        (< becomes &lt;, > becomes &gt;, etc. which can ~2-3x the size).
        """
        if len(text) <= max_len:
            return [text]

        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break

            # Find the last newline within the limit
            split_at = text.rfind("\n", 0, max_len)
            if split_at <= 0:
                # No newline found — split at max_len (last resort)
                split_at = max_len

            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")

        return chunks

    async def _send_response(self, placeholder, update: Update, markdown_response: str):
        """Send the response, chunking if needed, then convert to HTML.

        Chunks BEFORE HTML conversion to account for entity expansion.
        """
        # First chunk the markdown (before HTML conversion which expands entities)
        markdown_chunks = self._chunk_message(markdown_response, max_len=3000)
        chat_id = update.effective_chat.id if update.effective_chat else 0
        session_id = str(chat_id)

        for i, markdown_chunk in enumerate(markdown_chunks):
            self._log_bot_message(session_id, markdown_chunk)
            # Convert each chunk to HTML separately
            html_chunk = markdown_to_telegram_html(markdown_chunk)

            # Safety check: if HTML conversion made it too long, truncate
            if len(html_chunk) > 4096:
                html_chunk = html_chunk[:4050] + "..."

            if i == 0 and placeholder:
                # First chunk: edit the placeholder
                sent = await self._try_send(placeholder.edit_text, html_chunk)
                if sent:
                    continue
                # Edit failed — fall through to send as new message

            # Subsequent chunks or fallback: send as new message
            if update.message:
                await self._try_send(update.message.reply_text, html_chunk)

        # If we had multiple chunks, log it
        if len(markdown_chunks) > 1:
            log.info(f"Long response split into {len(markdown_chunks)} messages ({len(markdown_response)} chars)")

    @staticmethod
    def _is_large_code_leak(text: str) -> bool:
        """Detect suspicious large code dumps that should never reach chat."""
        if len(text) < 800:
            return False
        if "```" in text and any(tag in text.lower() for tag in ("```html", "```python", "```javascript", "```css", "```tsx", "```jsx")):
            return True
        indicators = ("<!doctype html", "<html", "tailwind.config", "function(", "className=", "import React", "def main(")
        return sum(1 for i in indicators if i.lower() in text.lower()) >= 2

    # ── Global Telegram Error Handler ────────────────────────

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Handle Telegram framework errors without noisy unstructured tracebacks."""
        err = context.error
        session_id = "unknown"
        if isinstance(update, Update):
            session_id = self._session_id_from_update(update)

        if isinstance(err, Conflict):
            now = time.time()
            # Polling conflicts repeat every few seconds; avoid log spam.
            if now - self._last_telegram_conflict_log_at >= 30:
                self._last_telegram_conflict_log_at = now
                log.warning(
                    f"[{session_id}] Telegram polling conflict: another bot instance is using getUpdates. "
                    "Keep only one `lightclaw run` active for this bot token."
                )
            return
        if isinstance(err, RetryAfter):
            log.warning(f"[{session_id}] Telegram rate limit: retry after {err.retry_after}s")
            return
        if isinstance(err, (TimedOut, NetworkError)):
            log.warning(f"[{session_id}] Telegram network issue: {err}")
            return

        log.exception(f"[{session_id}] Unhandled Telegram error", exc_info=err)

    async def _try_send(self, send_fn, text: str) -> bool:
        """Try to send/edit with HTML, fall back to plain text. Returns True on success."""
        try:
            await send_fn(text, parse_mode=ParseMode.HTML)
            return True
        except Exception:
            pass

        # Fallback: strip HTML tags and send as plain text
        try:
            plain = re.sub(r"<[^>]+>", "", text)
            await send_fn(plain)
            return True
        except Exception as e:
            log.error(f"Failed to send message chunk: {e}")
            return False


# ──────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────


def main():
    """Start the LightClaw Telegram bot."""
    config = load_config()

    # Resolve runtime paths relative to LIGHTCLAW_HOME (if set) or project root.
    config.workspace_path = str(resolve_runtime_path(config.workspace_path))
    config.memory_db_path = str(resolve_runtime_path(config.memory_db_path))
    config.skills_state_path = str(resolve_runtime_path(config.skills_state_path))

    # Ensure workspace directory exists
    workspace = Path(config.workspace_path)
    workspace.mkdir(parents=True, exist_ok=True)

    # Validate required config
    if not config.telegram_bot_token:
        log.error("TELEGRAM_BOT_TOKEN is required. Set it in .env")
        return

    if not config.llm_provider:
        log.error(
            "No LLM provider configured. Set LLM_PROVIDER and the corresponding API key in .env"
        )
        return

    log.info("🦞 LightClaw starting...")
    log.info(f"   Provider: {config.llm_provider} ({config.llm_model})")
    log.info(f"   Memory DB: {config.memory_db_path}")
    log.info(f"   Workspace: {config.workspace_path}")
    log.info(f"   Skills state: {config.skills_state_path}")
    log.info(f"   Skills hub: {config.skills_hub_base_url}")
    log.info(f"   Context window: {config.context_window:,} tokens")
    log.info(f"   Max output: {config.max_output_tokens:,} tokens")
    log.info(f"   Local agent timeout: {config.local_agent_timeout_sec}s")
    if config.groq_api_key:
        log.info("   Voice: ✅ Groq Whisper enabled")
    else:
        log.info("   Voice: ❌ disabled (set GROQ_API_KEY)")
    if config.telegram_allowed_users:
        log.info(f"   Allowed users: {', '.join(config.telegram_allowed_users)}")
    else:
        log.info("   Allowed users: everyone")

    bot = LightClawBot(config)

    # Print memory stats
    stats = bot.memory.stats()
    skill_count = len(bot.skills.list_skills())
    log.info(
        f"   Memory: {stats['total_interactions']} interactions, "
        f"{stats['unique_sessions']} sessions, "
        f"{stats['vocabulary_size']} vocabulary terms"
    )
    log.info(f"   Skills: {skill_count} installed")

    # Print personality source
    ws = Path(config.workspace_path)
    loaded = [f for f in ["IDENTITY.md", "SOUL.md", "USER.md"] if (ws / f).exists()]
    if loaded:
        log.info(f"   Personality: {', '.join(loaded)}")
    else:
        log.info("   Personality: built-in default")

    # Build Telegram application
    app = Application.builder().token(config.telegram_bot_token).build()

    # Register handlers
    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CommandHandler("clear", bot.cmd_clear))
    app.add_handler(CommandHandler("wipe_memory", bot.cmd_wipe_memory))
    app.add_handler(CommandHandler("wipe", bot.cmd_wipe_memory))
    app.add_handler(CommandHandler("memory", bot.cmd_memory))
    app.add_handler(CommandHandler("recall", bot.cmd_recall))
    app.add_handler(CommandHandler("skills", bot.cmd_skills))
    app.add_handler(CommandHandler("agent", bot.cmd_agent))
    app.add_handler(CommandHandler("show", bot.cmd_show))
    app.add_handler(MessageHandler(filters.VOICE, bot.handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, bot.handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, bot.handle_document))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message)
    )
    app.add_error_handler(bot.on_error)

    log.info("🦞 LightClaw is running! Press Ctrl+C to stop.")

    # Start polling
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
