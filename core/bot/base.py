"""Core bot base state and shared utility methods."""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode

from config import Config
from memory import MemoryStore
from providers import LLMClient
from skills import SkillManager

from ..constants import STRICT_LOCAL_AGENT_DENY_PATTERNS
from ..logging_setup import log
from ..personality import load_personality


class BotBaseMixin:
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
        # Per-chat file write mode (`chat`=read-only answers, `edit`=allow workspace writes).
        self._file_mode_by_session: dict[str, str] = {}
        # Backoff window to avoid repeated background LLM calls during provider failures.
        self._llm_backoff_until: float = 0.0
        # Throttle repeated Telegram polling conflict warnings.
        self._last_telegram_conflict_log_at: float = 0.0
        # Optional HEARTBEAT scheduler state (disabled by default).
        heartbeat_minutes = 15
        try:
            heartbeat_minutes = int(
                (os.getenv("HEARTBEAT_INTERVAL_MIN", "15") or "15").strip()
            )
        except Exception:
            heartbeat_minutes = 15
        self._heartbeat_enabled: bool = False
        self._heartbeat_interval_sec: int = max(5, heartbeat_minutes) * 60
        self._heartbeat_last_chat_id: str = ""
        self._heartbeat_last_run_at: float = 0.0
        self._heartbeat_task = None
        # Optional minimal cron scheduler state.
        self._cron_poll_sec: int = 30
        self._cron_last_run_at: float = 0.0
        self._cron_task = None
        self._cron_lock = asyncio.Lock()
        # Pending /agent multi plan proposals awaiting confirm/edit/cancel.
        self._pending_multi_plan_by_session: dict[str, dict[str, object]] = {}
        self._pending_multi_plan_ttl_sec: int = 15 * 60
        # Compiled strict-mode deny patterns for delegated local-agent tasks.
        self._delegation_deny_patterns = self._compile_delegation_deny_patterns()

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
        text = (user_text or "").strip()
        if not text:
            return False
        lower = text.lower()

        # Explicit fenced edit/file syntax from user.
        if "```edit:" in lower or re.search(r"```[a-z0-9_+\-]+:[^\n`]+", lower):
            return True

        # Remove common workspace task-folder slugs to avoid false positives
        # such as ".../20260227_120233_build-a-...".
        normalized = re.sub(r"\b\d{8}_\d{6}_[a-z0-9][a-z0-9_-]*\b", " ", lower)
        file_mentions = self._extract_file_mentions(text)
        if file_mentions:
            # Only treat file references as write-intent when paired with explicit change verbs.
            if re.search(
                r"\b(edit|modify|update|refactor|fix|patch|rewrite|create|write|add|remove|delete|implement|build|generate|make)\b",
                normalized,
            ):
                return True

        # Command-style coding/edit requests.
        command_patterns = (
            r"\b(build|create|generate|make|implement|write|code|develop|scaffold)\s+(a|an|the|this|that|it|me|new)\b",
            r"\b(edit|modify|update|refactor|fix|patch|rewrite)\b",
            r"\badd\s+(feature|tests?|docs?|endpoint|api|route|component|file|code)\b",
            r"\b(save|write)\s+(to|into)\s+[^\s]+",
            r"\bcreate\s+file\b",
        )
        return any(re.search(pattern, normalized) for pattern in command_patterns)

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
            transient_markers = (
                "connection error",
                "timed out",
                "timeout",
                "temporary failure",
                "temporarily unavailable",
                "name or service not known",
            )
            if any(marker in lower for marker in transient_markers):
                return False
            return True
        if lower.startswith("error communicating with"):
            transient_markers = (
                "connection error",
                "timed out",
                "timeout",
                "temporary failure",
                "temporarily unavailable",
                "name or service not known",
            )
            if any(marker in lower for marker in transient_markers):
                return False
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

    def _compile_delegation_deny_patterns(self) -> list[tuple[str, re.Pattern[str]]]:
        """Compile strict-mode deny patterns once at startup."""
        if self.config.local_agent_safety_mode != "strict":
            return []

        raw_patterns = list(STRICT_LOCAL_AGENT_DENY_PATTERNS)
        raw_patterns.extend(self.config.local_agent_deny_patterns)

        compiled: list[tuple[str, re.Pattern[str]]] = []
        for raw in raw_patterns:
            text = (raw or "").strip()
            if not text:
                continue
            try:
                compiled.append((text, re.compile(text, re.IGNORECASE)))
            except re.error:
                log.warning(f"Ignoring invalid LOCAL_AGENT_DENY_PATTERNS regex: {text}")
        return compiled

    def _delegation_safety_block_reason(self, task: str) -> str:
        """Return matched deny pattern if task is blocked, else empty string."""
        if self.config.local_agent_safety_mode != "strict":
            return ""

        task_text = task or ""
        for raw, pattern in self._delegation_deny_patterns:
            if pattern.search(task_text):
                return raw
        return ""

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

    def _get_file_mode(self, session_id: str) -> str:
        mode = (self._file_mode_by_session.get(session_id) or "chat").strip().lower()
        return "edit" if mode == "edit" else "chat"

    def _set_file_mode(self, session_id: str, mode: str) -> str:
        normalized = (mode or "").strip().lower()
        target = "edit" if normalized == "edit" else "chat"
        self._file_mode_by_session[session_id] = target
        return target

    def _set_pending_multi_plan(
        self,
        session_id: str,
        payload: dict[str, object],
        ttl_sec: int | None = None,
    ) -> dict[str, object]:
        ttl = max(30, int(ttl_sec or self._pending_multi_plan_ttl_sec))
        now = time.time()
        item = dict(payload or {})
        item["created_at"] = now
        item["expires_at"] = now + ttl
        self._pending_multi_plan_by_session[session_id] = item
        return item

    def _get_pending_multi_plan(self, session_id: str) -> dict[str, object] | None:
        entry = self._pending_multi_plan_by_session.get(session_id)
        if not entry:
            return None
        expires_at = float(entry.get("expires_at", 0.0) or 0.0)
        if expires_at <= 0 or time.time() > expires_at:
            self._pending_multi_plan_by_session.pop(session_id, None)
            return None
        return entry

    def _pending_multi_plan_remaining_sec(self, session_id: str) -> int:
        entry = self._get_pending_multi_plan(session_id)
        if not entry:
            return 0
        expires_at = float(entry.get("expires_at", 0.0) or 0.0)
        return max(0, int(expires_at - time.time()))

    def _clear_pending_multi_plan(self, session_id: str) -> dict[str, object] | None:
        return self._pending_multi_plan_by_session.pop(session_id, None)

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
