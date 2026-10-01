"""Core bot base state and shared utility methods."""

from __future__ import annotations

import asyncio
import heapq
import os
import re
import secrets
import stat
import time
import weakref
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode

from config import Config, heartbeat_interval_seconds, normalize_local_agent_timings
from memory import MemoryStore
from providers import LLMClient
from skills import SkillManager

from ..constants import STRICT_LOCAL_AGENT_DENY_PATTERNS
from ..jobs import JobStore
from ..logging_setup import log
from ..personality import load_personality
from ..security import access_policy_label, is_sensitive_path, redact_text
from .delegation.workspace import await_thread_completion

# ponytail: bounded in-process LRU; use shared storage if public traffic exceeds this ceiling.
MAX_PRIVILEGED_RATE_LIMIT_KEYS = 4096
_LEGACY_CANCELLED_TASKS = weakref.WeakSet()
_RECENT_FILE_SCAN_IGNORED_DIRS = frozenset(
    {".venv", "venv", "env", "node_modules", "__pycache__", ".tox", ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist"}
)


class BotBaseMixin:
    def __init__(self, config: Config):
        self.config = config
        self._heartbeat_interval_sec = heartbeat_interval_seconds(config.heartbeat_interval_min)
        normalize_local_agent_timings(config)
        with ExitStack() as cleanup:
            self.memory = MemoryStore(
                config.memory_db_path,
                retention_days=config.memory_retention_days,
                max_interactions=config.memory_max_interactions,
                max_db_bytes=config.memory_max_db_mb * 1024 * 1024,
                query_timeout_ms=config.memory_query_timeout_ms,
                candidate_limit=config.memory_candidate_limit,
            )
            cleanup.callback(self.memory.db.close)
            self.jobs = JobStore(Path(config.memory_db_path).expanduser().resolve().with_name("jobs.db"))
            cleanup.callback(self.jobs.close)
            self.jobs.recover_stalled()
            self.llm = LLMClient(config)
            cleanup.callback(self.llm.close)
            self.skills = SkillManager(
                workspace_path=config.workspace_path,
                skills_state_path=config.skills_state_path,
                hub_base_url=config.skills_hub_base_url,
            )
            self.personality = load_personality(config.workspace_path)
            self.start_time = time.time()

            # Per-session summaries (in-memory, persisted via memory.py)
            self._session_summaries: dict[tuple[str, str, str], str] = {}
            self._summary_generation_by_session: dict[tuple[str, str, str], int] = {}
            self._background_tasks: set[asyncio.Task] = set()
            # Lock to prevent concurrent summarization per session
            self._summarizing: set[tuple[str, str, str]] = set()
            # Global memory wipe confirmation uses the shared dual-clock approval contract.
            self._pending_wipe_confirm: dict[str, dict[str, object]] = {}
            # Track last successful file operation target per session.
            self._last_file_by_session: dict[str, str] = {}
            # Per-chat local delegation mode (codex/claude).
            self._agent_mode_by_session: dict[str, str] = {}
            # Per-chat file write mode (`chat`=read-only answers, `edit`=allow workspace writes).
            self._file_mode_by_session: dict[str, str] = {}
            # Backoff window to avoid repeated background LLM calls during provider failures.
            self._llm_backoff_until: float = 0.0
            # Throttle repeated Telegram polling conflict warnings.
            self._last_telegram_conflict_log_at: float = 0.0
            # Optional HEARTBEAT scheduler; /heartbeat on pins its chat (disabled by default).
            self._heartbeat_enabled: bool = False
            self._heartbeat_last_chat_id: str = ""
            self._heartbeat_last_run_at: float = 0.0
            self._heartbeat_task = None
            # Optional minimal cron scheduler state.
            self._cron_poll_sec: int = 30
            self._cron_last_run_at: float = 0.0
            self._cron_task = None
            self._cron_lock = asyncio.Lock()
            self._cron_iteration_lock = asyncio.Lock()
            # Track live/queued requests so /clear can invalidate their later memory writes.
            self._active_message_clear_events_by_session: dict[
                str, dict[asyncio.Task, asyncio.Event]
            ] = {}
            self._memory_wipe_lock = asyncio.Lock()
            # Pending /agent multi plan proposals awaiting confirm/edit/cancel.
            self._pending_multi_plan_by_session: dict[str, dict[str, object]] = {}
            self._pending_multi_plan_ttl_sec: int = 15 * 60
            # Explicit confirmation gate for trusted-command one-shot runs.
            self._pending_trusted_agent_run_by_session: dict[str, dict[str, object]] = {}
            # Explicit voice-transcription approval gate and live run controls.
            self._pending_voice_goal_by_session: dict[str, dict[str, object]] = {}
            self._voice_request_ids_by_session: dict[str, str] = {}
            self._active_run_tasks_by_session: dict[str, asyncio.Task[object]] = {}
            self._shutting_down = False
            self._active_run_ids_by_session: dict[str, str] = {}
            self._active_run_requesters_by_session: dict[str, int | None] = {}
            self._session_run_locks: dict[str, asyncio.Lock] = {}
            self._active_worker_tasks_by_run: dict[
                str, dict[asyncio.Task[object], str]
            ] = {}
            self._active_run_heartbeats_by_run: dict[str, asyncio.Task[None]] = {}
            self._last_run_ids_by_session: dict[str, str] = {}
            self._last_run_requesters_by_session: dict[str, int | None] = {}
            self._last_run_receipts_by_session: dict[str, str] = {}
            self._result_actions_in_flight: set[str] = set()
            # Sliding-window limiter for high-authority Telegram commands.
            self._privileged_request_times: dict[tuple[str, str], list[float]] = {}
            # Compiled strict-mode deny patterns for delegated local-agent tasks.
            self._delegation_deny_patterns = self._compile_delegation_deny_patterns()
            cleanup.pop_all()

    def close(self) -> None:
        """Close every resource, then surface the first shutdown error."""
        first_error: Exception | None = None
        for close in (self.llm.close, self.jobs.close, self.memory.db.close):
            try:
                close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _get_memory_wipe_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_memory_wipe_lock", None)
        if lock is None:
            lock = self._memory_wipe_lock = asyncio.Lock()
        return lock

    def _get_memory_write_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_memory_write_lock", None)
        if lock is None:
            lock = self._memory_write_lock = asyncio.Lock()
        return lock

    def _invalidate_active_message_requests(self, session_id: str | None = None) -> None:
        sessions = getattr(self, "_active_message_clear_events_by_session", {})
        for active_session, messages in sessions.items():
            if session_id is None or active_session == session_id:
                for clear_event in messages.values():
                    clear_event.set()

    @asynccontextmanager
    async def _memory_request_guard(self, session_id: str):
        current = asyncio.current_task()
        clear_event = asyncio.Event()
        registered_event = clear_event
        async with self._get_memory_wipe_lock():
            if getattr(self, "_shutting_down", False):
                raise asyncio.CancelledError
            active_messages = getattr(self, "_active_message_clear_events_by_session", None)
            if active_messages is None:
                active_messages = self._active_message_clear_events_by_session = {}
            session_messages = active_messages.setdefault(session_id, {})
            if current:
                clear_event = session_messages.setdefault(current, clear_event)
        try:
            yield clear_event
        finally:
            if current and clear_event is registered_event:
                session_messages.pop(current, None)
            if not session_messages:
                active_messages.pop(session_id, None)

    def _create_background_task(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)

        def discard_finished_task(finished: asyncio.Task) -> None:
            self._background_tasks.discard(finished)
            if not finished.cancelled() and (error := finished.exception()):
                log.error("Background bot task failed: %s", error)

        task.add_done_callback(discard_finished_task)
        return task

    def _cancel_task_once(self, task: asyncio.Task) -> None:
        if task.done():
            return
        cancelling = getattr(task, "cancelling", None)
        if cancelling is not None:
            if cancelling():
                return
        elif task in _LEGACY_CANCELLED_TASKS:
            return
        else:
            _LEGACY_CANCELLED_TASKS.add(task)
        task.cancel()

    async def shutdown(self) -> None:
        """Stop bot-owned background tasks before closing their dependencies."""
        active_requests = self._request_shutdown_cancellation()
        if active_requests:
            await asyncio.gather(*active_requests, return_exceptions=True)
        self._heartbeat_enabled = False
        tasks = set(self._background_tasks)
        tasks.update(
            task
            for task in (self._heartbeat_task, self._cron_task)
            if task is not None
        )
        self._heartbeat_task = None
        self._cron_task = None
        for task in tasks:
            self._cancel_task_once(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._get_memory_wipe_lock():
            self.close()

    def _request_shutdown_cancellation(self) -> set[asyncio.Task[object]]:
        self._shutting_down = True
        current = asyncio.current_task()
        tasks = {
            task
            for task in getattr(self, "_active_run_tasks_by_session", {}).values()
            if not task.done() and task is not current
        }
        tasks.update(
            task
            for messages in getattr(self, "_active_message_clear_events_by_session", {}).values()
            for task in messages
            if task is not current
        )
        for task in tasks:
            self._cancel_task_once(task)
        return tasks

    def is_allowed(self, user_id: int) -> bool:
        """Fail closed unless an allowlist or explicit public override exists."""
        if self.config.telegram_allowed_users:
            return str(user_id) in self.config.telegram_allowed_users
        return bool(self.config.telegram_public_bot_ack)

    def is_update_allowed(self, update: Update | None) -> bool:
        """Keep allowlisted sessions private; public mode explicitly permits groups."""
        if (
            getattr(self, "_shutting_down", False)
            or not update
            or not update.effective_user
            or not update.effective_chat
        ):
            return False
        if not self.is_allowed(update.effective_user.id):
            return False
        if update.effective_chat.type == "private":
            return True
        return bool(
            self.config.telegram_public_bot_ack
            and not self.config.telegram_allowed_users
        )

    @staticmethod
    def _is_pending_requester(update: Update, pending: dict[str, object]) -> bool:
        user = getattr(update, "effective_user", None)
        return bool(user and pending.get("user_id") == user.id)

    def _pending_action_is_owned_by_other(
        self, update: Update, pending: dict[str, object] | None
    ) -> bool:
        return bool(
            pending
            and (
                pending.get("transcribing") is True
                or not self._pending_confirmation_expired(pending)
            )
            and not self._is_pending_requester(update, pending)
        )

    def _access_policy_label(self) -> str:
        return access_policy_label(
            self.config.telegram_allowed_users,
            self.config.telegram_public_bot_ack,
        )

    def _privileged_rate_limited(
        self,
        user_id: int,
        action: str,
        *,
        limit: int = 8,
        window_sec: int = 60,
    ) -> bool:
        """Return True when a privileged action exceeds its sliding-window limit."""
        now = time.monotonic()
        key = (str(user_id), str(action))
        cutoff = now - max(1, int(window_sec))
        recent = [stamp for stamp in self._privileged_request_times.get(key, []) if stamp >= cutoff]
        limited = len(recent) >= max(1, int(limit))
        if not limited:
            recent.append(now)
        self._privileged_request_times.pop(key, None)
        self._privileged_request_times[key] = recent
        while len(self._privileged_request_times) > MAX_PRIVILEGED_RATE_LIMIT_KEYS:
            self._privileged_request_times.pop(next(iter(self._privileged_request_times)))
        return limited

    def _session_id_from_update(self, update: Update | None) -> str:
        if update and update.effective_chat:
            message = getattr(update, "effective_message", None) or getattr(
                update, "message", None
            )
            topic_id = getattr(message, "message_thread_id", None)
            if type(topic_id) is int and topic_id > 0:
                return f"{update.effective_chat.id}:topic:{topic_id}"
            return str(update.effective_chat.id)
        return "unknown"

    def _memory_recall_current_session_only(self, update: Update | None) -> bool:
        chat = getattr(update, "effective_chat", None)
        if not chat:
            return True
        if getattr(chat, "type", "private") != "private":
            return True
        message = getattr(update, "effective_message", None) or getattr(
            update, "message", None
        )
        topic_id = getattr(message, "message_thread_id", None)
        return type(topic_id) is int and topic_id > 0

    @staticmethod
    def _telegram_target_from_session_id(
        session_id: str,
    ) -> tuple[int, int | None] | None:
        """Parse persisted Telegram session IDs for scheduled delivery."""
        raw = str(session_id).strip()
        chat_raw, separator, topic_raw = raw.rpartition(":topic:")
        try:
            chat_id = int(chat_raw if separator else raw)
            topic_id = int(topic_raw) if separator else None
        except ValueError:
            return None
        if chat_id == 0 or (topic_id is not None and topic_id <= 0):
            return None
        return chat_id, topic_id

    async def _session_scope_from_update(self, update: Update | None) -> str:
        session_id = self._session_id_from_update(update)
        user = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        memory = getattr(self, "memory", None)
        bind_session = getattr(memory, "bind_session", None)
        workspace_path = getattr(getattr(self, "config", None), "workspace_path", None)
        if (
            not update
            or not user
            or not chat
            or not callable(bind_session)
            or workspace_path is None
        ):
            return session_id

        user_namespace = f"telegram-user:{user.id}"
        scope = {
            "user_namespace": user_namespace,
            "workspace_namespace": Path(str(workspace_path)).expanduser().resolve().as_posix(),
        }
        activate_scope = getattr(memory, "activate_scope", None)
        if callable(activate_scope):
            activate_scope(session_id, **scope)
        await await_thread_completion(bind_session, session_id, **scope)
        return session_id

    @staticmethod
    def _trim_for_log(text: str, max_chars: int = 8000) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n...[truncated]"

    @staticmethod
    def _strip_html_for_log(text: str) -> str:
        return re.sub(r"<[^>]+>", "", text)

    def _log_user_message(self, session_id: str, text: str):
        # Prompt content and Telegram identifiers are private by default. Detailed
        # run evidence belongs in local receipts, not the process log.
        log.info("User message received")

    def _log_bot_message(self, session_id: str, text: str):
        log.info("Bot response emitted")

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
            if not err and target and rel_path and not is_sensitive_path(rel_path):
                candidates.append(rel_path)

        # 2) Last touched file in this chat.
        last = self._last_file_by_session.get(session_id)
        if last:
            target, rel_path, err = self._resolve_workspace_path(last)
            if (
                not err
                and target
                and rel_path
                and not is_sensitive_path(rel_path)
                and target.exists()
            ):
                candidates.append(rel_path)

        # 3) Most recently modified workspace files.
        workspace = Path(self.config.workspace_path).resolve()

        def recent_files():
            for root, directories, filenames in os.walk(workspace):
                directories[:] = [
                    name for name in directories
                    if name.lower() not in _RECENT_FILE_SCAN_IGNORED_DIRS
                    and not is_sensitive_path(name)
                ]
                root_path = Path(root)
                for filename in filenames:
                    path = root_path / filename
                    rel_path = path.relative_to(workspace).as_posix()
                    if is_sensitive_path(rel_path):
                        continue
                    try:
                        metadata = path.lstat()
                    except OSError:
                        continue
                    if stat.S_ISREG(metadata.st_mode):
                        yield metadata.st_mtime, path

        for _, path in heapq.nlargest(
            20, recent_files(), key=lambda item: (item[0], item[1].as_posix())
        ):
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
        item["approval_id"] = secrets.token_hex(8)
        item["review_delivered"] = False
        item["created_at"] = now
        item["expires_at"] = now + ttl
        item["expires_monotonic"] = time.monotonic() + ttl
        self._pending_multi_plan_by_session[session_id] = item
        return item

    @staticmethod
    def _pending_confirmation_expired(pending: dict[str, object]) -> bool:
        try:
            wall_deadline = float(pending.get("expires_at", 0) or 0)
            monotonic_deadline = float(pending.get("expires_monotonic", 0) or 0)
        except (TypeError, ValueError):
            return True
        return (
            wall_deadline <= 0
            or monotonic_deadline <= 0
            or time.time() >= wall_deadline
            or time.monotonic() >= monotonic_deadline
        )

    def _get_pending_multi_plan(self, session_id: str) -> dict[str, object] | None:
        entry = self._pending_multi_plan_by_session.get(session_id)
        if not entry:
            return None
        if self._pending_confirmation_expired(entry):
            self._pending_multi_plan_by_session.pop(session_id, None)
            return None
        if entry.get("planning"):
            return None
        return entry

    def _pending_multi_plan_remaining_sec(self, session_id: str) -> int:
        entry = self._get_pending_multi_plan(session_id)
        if not entry:
            return 0
        wall_left = float(entry["expires_at"]) - time.time()
        monotonic_left = float(entry["expires_monotonic"]) - time.monotonic()
        return max(0, int(min(wall_left, monotonic_left)))

    def _clear_pending_multi_plan(self, session_id: str) -> dict[str, object] | None:
        return self._pending_multi_plan_by_session.pop(session_id, None)

    def _clear_pending_actions(
        self,
        session_id: str | None = None,
        *,
        requester_user_id: int | None = None,
    ) -> None:
        pending_maps = (
            self._pending_wipe_confirm,
            self._pending_multi_plan_by_session,
            self._pending_trusted_agent_run_by_session,
            self._pending_voice_goal_by_session,
        )
        for pending in pending_maps:
            if session_id is None:
                pending.clear()
            elif requester_user_id is None:
                pending.pop(session_id, None)
            else:
                action = pending.get(session_id)
                if isinstance(action, dict) and action.get("user_id") == requester_user_id:
                    pending.pop(session_id, None)
        if session_id is None:
            self._voice_request_ids_by_session.clear()
        elif requester_user_id is None or session_id not in self._pending_voice_goal_by_session:
            self._voice_request_ids_by_session.pop(session_id, None)

    async def _reply_logged(
        self,
        update: Update,
        text: str,
        parse_mode: str | None = None,
        reply_markup=None,
    ):
        """Reply to Telegram and mirror the same content to terminal logs."""
        text = redact_text(text, getattr(getattr(self, "config", None), "__dict__", {}))
        session_id = self._session_id_from_update(update)
        logged_text = self._strip_html_for_log(text) if parse_mode == ParseMode.HTML else text
        self._log_bot_message(session_id, logged_text)

        if parse_mode:
            return await update.message.reply_text(
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        return await update.message.reply_text(text, reply_markup=reply_markup)

    # ── Token Estimation ─────────────────────────────────────
