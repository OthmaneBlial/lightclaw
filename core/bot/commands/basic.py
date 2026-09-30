"""Core chat command handlers and baseline mode/memory commands."""

from __future__ import annotations

import asyncio
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ...markdown import _escape_html


class CommandsBasicMixin:
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_update_allowed(update):
            return

        session_id = self._session_id_from_update(update)
        self._log_user_message(session_id, "/start")
        await self._reply_logged(
            update,
            "🦞 <b>LightClaw</b> is ready!\n\n"
            "I'm your AI assistant with bounded, local lexical memory. "
            "Recall is isolated by user and workspace and follows configured retention.\n\n"
            "<b>Commands:</b>\n"
            "/help - Show this message\n"
            "/clear - Clear this chat's history and pending approvals\n"
            "/wipe_memory - Wipe all memory and pending approvals (active runs continue)\n"
            "/memory - Show memory stats\n"
            "/recall &lt;query&gt; - Search my memories\n"
            "/skills - Manage skills (install/use/create)\n"
            "/agent - Delegate tasks to local coding agents\n"
            "/agent multi - Auto-plan multi-agent run with confirm/edit/cancel\n"
            "/agent doctor - Check local agent install/auth health\n"
            "/mode - File write mode (chat/edit)\n"
            "/heartbeat - HEARTBEAT.md scheduler (on/off/show)\n"
            "/cron - Minimal scheduler (add/list/remove)\n"
            "/show - Show current config",
            parse_mode=ParseMode.HTML,
        )


    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_update_allowed(update):
            return

        session_id = self._session_id_from_update(update)
        self._log_user_message(session_id, "/help")
        await self._reply_logged(
            update,
            "🦞 <b>LightClaw Commands</b>\n\n"
            "/start - Welcome message\n"
            "/help - This help message\n"
            "/clear - Clear this chat's history and pending approvals\n"
            "/wipe_memory - Wipe all memory and pending approvals (active runs continue)\n"
            "/memory - Show memory statistics\n"
            "/recall &lt;query&gt; - Search past conversations\n"
            "/skills - Install/use/create skills\n"
            "/agent - Delegate tasks to local coding agents\n"
            "/agent multi - Auto-plan multi-agent run with confirm/edit/cancel\n"
            "/agent doctor - Check local agent install/auth health\n"
            "/mode - File write mode (chat/edit)\n"
            "/heartbeat - HEARTBEAT.md scheduler (on/off/show)\n"
            "/cron - Minimal scheduler (add/list/remove)\n"
            "/show - Show current model, provider, uptime",
            parse_mode=ParseMode.HTML,
        )


    async def cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_update_allowed(update):
            return
        if self._privileged_rate_limited(update.effective_user.id, "wipe-memory", limit=4):
            await self._reply_logged(
                update,
                "⚠️ Too many destructive requests. Retry in about one minute.",
            )
            return

        session_id = self._session_id_from_update(update)
        self._log_user_message(session_id, "/clear")
        for clear_event in getattr(
            self, "_active_message_clear_events_by_session", {}
        ).get(
            session_id, {}
        ).values():
            clear_event.set()
        self._clear_pending_actions(session_id)
        self._invalidate_session_summary(session_id)
        self.memory.clear_session(session_id)
        self._session_summaries.pop(self._summary_key(session_id), None)
        await self._reply_logged(
            update,
            "🗑️ Conversation cleared. Pending approvals and confirmations were discarded.\n"
            "Active runs continue; in-flight chat replies may finish but won't be saved. "
            "Use a run's Cancel button to stop it. "
            "Memories from other chats are preserved."
        )


    async def cmd_wipe_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Dangerous command: wipe all memory after explicit confirmation."""
        if not update.effective_user or not update.message:
            return
        if not self.is_update_allowed(update):
            return

        session_id = self._session_id_from_update(update)
        args = [a.strip().lower() for a in (context.args or []) if a.strip()]
        self._log_user_message(session_id, f"/wipe_memory {' '.join(args)}".strip())

        now = time.time()
        confirm_window_sec = 90
        pending = self._pending_wipe_confirm.get(session_id)
        confirmation_active = bool(
            pending
            and pending.get("user_id") == update.effective_user.id
            and not self._pending_confirmation_expired(pending)
        )

        if args and args[0] in {"confirm", "yes", "now"}:
            if confirmation_active:
                self._clear_pending_actions()
                self._invalidate_active_summaries()
                await asyncio.to_thread(self.memory.clear_all)
                self._invalidate_active_summaries()
                self._session_summaries.clear()
                await self._reply_logged(
                    update,
                    "🧨 <b>All memory wiped.</b>\n"
                    "Saved interactions and pending approvals were deleted across chats.\n"
                    "Already-active runs continue; cancel them from their chat.",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await self._reply_logged(
                    update,
                    "No active wipe confirmation for your Telegram user.\n"
                    "Run <code>/wipe_memory</code> first, then confirm within 90s with "
                    "<code>/wipe_memory confirm</code>.",
                    parse_mode=ParseMode.HTML,
                )
            return

        self._pending_wipe_confirm[session_id] = {
            "user_id": update.effective_user.id,
            "expires_at": now + confirm_window_sec,
            "expires_monotonic": time.monotonic() + confirm_window_sec,
        }
        await self._reply_logged(
            update,
            "⚠️ <b>Danger: wipe ALL memory</b>\n"
            "This deletes every saved interaction and session across all chats.\n\n"
            f"To confirm within {confirm_window_sec}s from this Telegram user, run:\n"
            "<code>/wipe_memory confirm</code>",
            parse_mode=ParseMode.HTML,
        )


    async def cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_update_allowed(update):
            return

        session_id = self._session_id_from_update(update)
        self._log_user_message(session_id, "/memory")
        stats = self.memory.stats(session_id=session_id)
        await self._reply_logged(
            update,
            f"🧠 <b>Memory Stats</b>\n\n"
            f"📝 Total interactions: {stats['total_interactions']}\n"
            f"💬 Unique sessions: {stats['unique_sessions']}\n"
            f"🔎 Retrieval: {stats['retrieval']}\n"
            f"💾 Database: {stats['database_bytes']} / {stats['max_database_bytes']} bytes\n"
            f"⏱️ Last query: {stats['last_query_ms']} ms (limit {stats['query_timeout_ms']} ms)",
            parse_mode=ParseMode.HTML,
        )


    async def cmd_recall(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_update_allowed(update):
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

        memories = self.memory.recall(query, top_k=5, session_id=session_id)
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


    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_update_allowed(update):
            return

        session_id = self._session_id_from_update(update)
        raw = " ".join(context.args or []).strip().lower()
        self._log_user_message(session_id, f"/mode {raw}".strip())

        if not raw:
            mode = self._get_file_mode(session_id)
            await self._reply_logged(
                update,
                "🧭 <b>File Write Mode</b>\n\n"
                f"<b>Current:</b> <code>{_escape_html(mode)}</code>\n\n"
                "<b>Modes:</b>\n"
                "• <code>chat</code> — never write workspace files from normal chat replies\n"
                "• <code>edit</code> — allow file writes when prompt is coding/edit intent\n\n"
                "Use:\n"
                "<code>/mode chat</code>\n"
                "<code>/mode edit</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        if raw not in {"chat", "edit"}:
            await self._reply_logged(
                update,
                "Usage: <code>/mode chat</code> or <code>/mode edit</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        active = self._set_file_mode(session_id, raw)
        if active == "chat":
            await self._reply_logged(
                update,
                "✅ File write mode set to <code>chat</code>.\n"
                "Normal chat replies will stay in chat without creating files.",
                parse_mode=ParseMode.HTML,
            )
            return

        await self._reply_logged(
            update,
            "✅ File write mode set to <code>edit</code>.\n"
            "Coding/edit prompts can now write files in the workspace.",
            parse_mode=ParseMode.HTML,
        )
