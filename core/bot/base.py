"""Core bot base state and shared utility methods."""

from __future__ import annotations

import re
import shutil
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
        # Backoff window to avoid repeated background LLM calls during provider failures.
        self._llm_backoff_until: float = 0.0
        # Throttle repeated Telegram polling conflict warnings.
        self._last_telegram_conflict_log_at: float = 0.0
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

