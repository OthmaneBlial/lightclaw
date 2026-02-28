"""Core chat command handlers and baseline mode/memory commands."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from skills import SkillError

from ...logging_setup import log
from ...markdown import _escape_html, markdown_to_telegram_html
from ...personality import build_system_prompt, runtime_root_from_workspace

class CommandsBasicMixin:
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


    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
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

