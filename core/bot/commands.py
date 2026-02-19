"""Telegram command handlers for /start, /help, /skills, and /agent."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from skills import SkillError

from ..logging_setup import log
from ..markdown import _escape_html, markdown_to_telegram_html
from ..personality import build_system_prompt, runtime_root_from_workspace


class BotCommandsMixin:
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
            "/heartbeat - HEARTBEAT.md scheduler (on/off/show)\n"
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
            "/heartbeat - HEARTBEAT.md scheduler (on/off/show)\n"
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

    # ── /heartbeat ────────────────────────────────────────────

    @staticmethod
    def _heartbeat_usage_text() -> str:
        return (
            "<b>Usage</b>\n"
            "<code>/heartbeat show</code> - show scheduler status\n"
            "<code>/heartbeat on [minutes]</code> - enable heartbeat (min 5 minutes)\n"
            "<code>/heartbeat off</code> - disable heartbeat"
        )

    def _heartbeat_file_path(self) -> Path:
        runtime_root = runtime_root_from_workspace(self.config.workspace_path)
        return runtime_root / "HEARTBEAT.md"

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        value = max(0, int(seconds))
        if value < 60:
            return f"{value}s"
        if value < 3600:
            return f"{value // 60}m"
        return f"{value // 3600}h"

    def _render_heartbeat_status(self) -> str:
        interval_min = max(5, int(self._heartbeat_interval_sec // 60))
        mode = "on" if self._heartbeat_enabled else "off"
        task_state = (
            "running" if self._heartbeat_task and not self._heartbeat_task.done() else "stopped"
        )
        target = self._heartbeat_last_chat_id or "none"
        heartbeat_path = self._heartbeat_file_path()
        exists = "yes" if heartbeat_path.exists() else "no"
        last_run = "never"
        if self._heartbeat_last_run_at > 0:
            last_run = f"{self._format_elapsed(time.time() - self._heartbeat_last_run_at)} ago"

        lines = [
            "💓 <b>Heartbeat Scheduler</b>",
            "",
            f"<b>Mode:</b> <code>{_escape_html(mode)}</code>",
            f"<b>Loop task:</b> <code>{_escape_html(task_state)}</code>",
            f"<b>Interval:</b> <code>{interval_min}m</code> (min 5m)",
            f"<b>Last active chat:</b> <code>{_escape_html(target)}</code>",
            f"<b>Last run:</b> <code>{_escape_html(last_run)}</code>",
            f"<b>HEARTBEAT.md:</b> <code>{_escape_html(heartbeat_path.as_posix())}</code> (exists: {exists})",
            "",
            self._heartbeat_usage_text(),
        ]
        return "\n".join(lines)

    async def _ensure_heartbeat_task(self, bot):
        if self._heartbeat_task and not self._heartbeat_task.done():
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(bot))

    def _stop_heartbeat_task(self):
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task and not task.done():
            task.cancel()

    async def _heartbeat_loop(self, bot):
        try:
            while self._heartbeat_enabled:
                await asyncio.sleep(max(300, int(self._heartbeat_interval_sec)))
                if not self._heartbeat_enabled:
                    break
                session_id = (self._heartbeat_last_chat_id or "").strip()
                if not session_id:
                    continue
                await self._run_heartbeat_once(bot, session_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"Heartbeat scheduler stopped due to error: {e}")
        finally:
            self._heartbeat_task = None

    async def _run_heartbeat_once(self, bot, session_id: str):
        heartbeat_path = self._heartbeat_file_path()
        if not heartbeat_path.exists():
            return

        try:
            heartbeat_body = heartbeat_path.read_text(encoding="utf-8").strip()
        except Exception as e:
            log.error(f"[{session_id}] Failed to read HEARTBEAT.md: {e}")
            return

        if not heartbeat_body:
            return

        if self._llm_backoff_active():
            return

        try:
            chat_id = int(session_id)
        except ValueError:
            # Terminal chat sessions may use non-numeric IDs.
            return

        memories = self.memory.recall(
            "heartbeat automation",
            top_k=max(1, min(self.config.memory_top_k, 4)),
        )
        memories = self._filter_recalled_memories(memories)
        memories_text = self.memory.format_memories_for_prompt(memories)
        summary = self._get_session_summary(session_id)
        skills_text = await asyncio.to_thread(self.skills.prompt_context, session_id)
        system_prompt = build_system_prompt(
            self.config, self.personality, memories_text, summary, skills_text
        )

        heartbeat_prompt = (
            "This is a scheduled HEARTBEAT run.\n"
            "Read HEARTBEAT.md below and execute it now.\n"
            "Return a concise user-facing update.\n"
            "If there is nothing useful to report right now, respond exactly with: NO_UPDATE\n\n"
            "HEARTBEAT.md:\n"
            f"{heartbeat_body}\n"
        )

        try:
            response = await self.llm.chat(
                [{"role": "user", "content": heartbeat_prompt}],
                system_prompt=system_prompt,
            )
        except Exception as e:
            log.error(f"[{session_id}] Heartbeat LLM call failed: {e}")
            return

        if not response:
            return
        if self._is_provider_error_text(response):
            self._set_llm_backoff()
            return
        self._clear_llm_backoff()

        if response.strip().upper() == "NO_UPDATE":
            return

        file_ops, cleaned_response = await self._process_file_blocks(response)
        repair_ops = await self._repair_incomplete_html(session_id, heartbeat_prompt, file_ops)
        if repair_ops:
            repaired_paths = {op.path for op in repair_ops if op.action != "error"}
            if repaired_paths:
                file_ops = [op for op in file_ops if op.path not in repaired_paths]
            file_ops.extend(repair_ops)

        success_ops = [op for op in file_ops if op.action != "error" and op.path]
        if not success_ops:
            force_prompt = (
                "Heartbeat follow-up.\n"
                "If HEARTBEAT.md requires creating or editing files, return ONLY valid file blocks now.\n"
                "Allowed formats:\n"
                "1) ```lang:path/to/file.ext ...```\n"
                "2) ```edit:path/to/file.ext ...```\n"
                "Do not include prose outside blocks.\n"
                "If no file updates are needed, respond exactly with: NO_UPDATE\n\n"
                "HEARTBEAT.md:\n"
                f"{heartbeat_body}\n"
            )
            try:
                forced_response = await self.llm.chat(
                    [{"role": "user", "content": force_prompt}],
                    system_prompt=system_prompt,
                )
            except Exception as e:
                log.error(f"[{session_id}] Heartbeat file-op follow-up failed: {e}")
                forced_response = ""

            if forced_response and not self._is_provider_error_text(forced_response):
                if forced_response.strip().upper() == "NO_UPDATE":
                    cleaned_response = "No file changes this run."
                else:
                    forced_ops, forced_cleaned = await self._process_file_blocks(forced_response)
                    forced_repair_ops = await self._repair_incomplete_html(
                        session_id, force_prompt, forced_ops
                    )
                    if forced_repair_ops:
                        repaired_paths = {
                            op.path for op in forced_repair_ops if op.action != "error"
                        }
                        if repaired_paths:
                            forced_ops = [
                                op for op in forced_ops if op.path not in repaired_paths
                            ]
                        forced_ops.extend(forced_repair_ops)

                    forced_success = [
                        op for op in forced_ops if op.action != "error" and op.path
                    ]
                    if forced_success:
                        file_ops = forced_ops
                        cleaned_response = forced_cleaned
                        success_ops = forced_success

        workspace_label = self._workspace_display_path()
        if success_ops:
            paths = [op.path for op in success_ops if op.path]
            preview = ", ".join(f"`{path}`" for path in paths[:3])
            if len(paths) > 3:
                preview += f", +{len(paths) - 3} more"
            final_response = (
                f"Created {len(paths)} file(s) in `{workspace_label}`.\n"
                f"Files: {preview}"
            )
        else:
            compact = self._compact_response_for_file_ops(cleaned_response)
            compact = compact.strip() or "No file changes this run."
            if len(compact) > 240:
                compact = compact[:237].rstrip() + "..."
            final_response = compact

        final_markdown = f"💓 Heartbeat update\n\n{final_response}"
        chunks = self._chunk_message(final_markdown, max_len=3000)

        async def _send_message(text: str, parse_mode: str | None = None):
            return await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)

        sent_ok = False
        for chunk in chunks:
            html_chunk = markdown_to_telegram_html(chunk)
            if await self._try_send(_send_message, html_chunk):
                sent_ok = True

        if not sent_ok:
            return

        self._heartbeat_last_run_at = time.time()
        self.memory.ingest("assistant", f"[heartbeat]\n\n{final_response}", session_id)

    async def cmd_heartbeat(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        session_id = self._session_id_from_update(update)
        self._heartbeat_last_chat_id = session_id
        args = context.args or []
        self._log_user_message(session_id, f"/heartbeat {' '.join(args)}".strip())

        sub = (args[0].strip().lower() if args else "show")

        if sub in {"show", "status"}:
            await self._reply_logged(
                update,
                self._render_heartbeat_status(),
                parse_mode=ParseMode.HTML,
            )
            return

        if sub in {"off", "disable", "stop"}:
            was_on = self._heartbeat_enabled
            self._heartbeat_enabled = False
            self._stop_heartbeat_task()
            note = "disabled" if was_on else "already off"
            await self._reply_logged(
                update,
                f"💓 Heartbeat {note}.",
            )
            return

        if sub in {"on", "enable", "start"}:
            if not hasattr(context.bot, "send_message"):
                await self._reply_logged(
                    update,
                    "Heartbeat scheduler requires Telegram runtime (send_message API unavailable).",
                )
                return

            interval_min = max(5, int(self._heartbeat_interval_sec // 60))
            if len(args) >= 2:
                raw = args[1].strip()
                try:
                    interval_min = max(5, int(raw))
                except ValueError:
                    await self._reply_logged(
                        update,
                        "Usage: <code>/heartbeat on [minutes]</code> (minutes must be a number, min 5)",
                        parse_mode=ParseMode.HTML,
                    )
                    return

            self._heartbeat_interval_sec = max(5, interval_min) * 60
            self._heartbeat_enabled = True
            await self._ensure_heartbeat_task(context.bot)

            heartbeat_path = self._heartbeat_file_path()
            file_hint = (
                f"Found <code>{_escape_html(heartbeat_path.as_posix())}</code>."
                if heartbeat_path.exists()
                else (
                    f"No <code>{_escape_html(heartbeat_path.as_posix())}</code> yet. "
                    "Create it to define heartbeat behavior."
                )
            )
            await self._reply_logged(
                update,
                "\n".join(
                    [
                        f"💓 Heartbeat enabled every <code>{interval_min}m</code>.",
                        f"Target chat: <code>{_escape_html(session_id)}</code>",
                        file_hint,
                    ]
                ),
                parse_mode=ParseMode.HTML,
            )
            return

        await self._reply_logged(
            update,
            "Unknown /heartbeat subcommand.\n\n" + self._heartbeat_usage_text(),
            parse_mode=ParseMode.HTML,
        )
