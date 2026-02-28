"""Telegram command handlers for /start, /help, /skills, and /agent."""

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

from ..logging_setup import log
from ..markdown import _escape_html, markdown_to_telegram_html
from ..personality import build_system_prompt, runtime_root_from_workspace


def _atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Atomically write text to disk using fsync + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_path = Path(tmp.name)

        os.replace(temp_path, path)
        temp_path = None

        # Best-effort fsync of parent directory metadata.
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True))


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
            "/agent multi - Auto-plan multi-agent run with confirm/edit/cancel\n"
            "/agent doctor - Check local agent install/auth health\n"
            "/mode - File write mode (chat/edit)\n"
            "/heartbeat - HEARTBEAT.md scheduler (on/off/show)\n"
            "/cron - Minimal scheduler (add/list/remove)\n"
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
            "/agent multi - Auto-plan multi-agent run with confirm/edit/cancel\n"
            "/agent doctor - Check local agent install/auth health\n"
            "/mode - File write mode (chat/edit)\n"
            "/heartbeat - HEARTBEAT.md scheduler (on/off/show)\n"
            "/cron - Minimal scheduler (add/list/remove)\n"
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

    # ── /mode ────────────────────────────────────────────────

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
                    "Usage: <code>/agent use &lt;codex|claude&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            agent = self._resolve_local_agent_name(args[1])
            if not agent:
                await self._reply_logged(
                    update,
                    "Unknown agent. Use one of: <code>codex</code>, "
                    "<code>claude</code>.",
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
            self._clear_pending_multi_plan(session_id)
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

        if sub == "multi":
            parsed, parse_error = self._parse_multi_agent_args(args[1:])
            if parse_error:
                await self._reply_logged(
                    update,
                    parse_error,
                    parse_mode=ParseMode.HTML,
                )
                return

            action = str(parsed.get("action") or "")
            pending = self._get_pending_multi_plan(session_id)

            if action == "confirm":
                if not pending:
                    await self._reply_logged(
                        update,
                        "No pending multi-agent plan.\nStart one with <code>/agent multi &lt;goal&gt;</code>.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                await self._execute_pending_multi_plan(update, session_id)
                return

            if action == "cancel":
                cleared = self._clear_pending_multi_plan(session_id)
                if not cleared:
                    await self._reply_logged(
                        update,
                        "No pending multi-agent plan to cancel.",
                    )
                    return
                await self._reply_logged(update, "Cancelled pending multi-agent plan.")
                return

            if action == "edit":
                if not pending:
                    await self._reply_logged(
                        update,
                        "No pending multi-agent plan.\nStart one with <code>/agent multi &lt;goal&gt;</code>.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                feedback = str(parsed.get("feedback") or "").strip()
                goal = str(pending.get("goal") or "")
                explicit_specs = (
                    pending.get("explicit_specs")
                    if isinstance(pending.get("explicit_specs"), list)
                    else []
                )
                preferred_agents = (
                    pending.get("preferred_agents")
                    if isinstance(pending.get("preferred_agents"), list)
                    else []
                )
                explicit_pairs: list[tuple[str, str]] = []
                for item in explicit_specs:
                    if not isinstance(item, (list, tuple)) or len(item) != 2:
                        continue
                    label = str(item[0]).strip()
                    agent = str(item[1]).strip()
                    if label and agent:
                        explicit_pairs.append((label, agent))
                available = self._available_local_agents()
                planned, plan_error = await self._plan_multi_agent_payload(
                    goal=goal,
                    available_agents=available,
                    explicit_specs=explicit_pairs,
                    preferred_agents=[str(a) for a in preferred_agents if isinstance(a, str)],
                    feedback=feedback,
                )
                if plan_error:
                    await self._reply_logged(update, plan_error, parse_mode=ParseMode.HTML)
                    return
                pending_payload = self._set_pending_multi_plan(
                    session_id,
                    {
                        **planned,
                        "feedback": feedback,
                    },
                )
                preview_payload_obj = pending_payload.get("plan_payload")
                preview_payload = (
                    preview_payload_obj
                    if isinstance(preview_payload_obj, dict)
                    else {}
                )
                preview_warnings_obj = pending_payload.get("warnings")
                preview_warnings = (
                    preview_warnings_obj
                    if isinstance(preview_warnings_obj, list)
                    else []
                )
                preview = self._render_multi_plan_preview(
                    goal=str(pending_payload.get("goal") or ""),
                    workers=list(pending_payload.get("workers") or []),
                    plan_payload=preview_payload,
                    warnings=[str(item) for item in preview_warnings],
                    include_confirm_hint=True,
                )
                await self._reply_logged(update, preview, parse_mode=ParseMode.HTML)
                return

            goal = str(parsed.get("goal") or "").strip()
            explicit_specs_obj = parsed.get("explicit_specs")
            explicit_specs = explicit_specs_obj if isinstance(explicit_specs_obj, list) else []
            preferred_agents_obj = parsed.get("preferred_agents")
            preferred_agents = preferred_agents_obj if isinstance(preferred_agents_obj, list) else []
            explicit_pairs: list[tuple[str, str]] = []
            for item in explicit_specs:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    continue
                label = str(item[0]).strip()
                agent = str(item[1]).strip()
                if label and agent:
                    explicit_pairs.append((label, agent))

            available = self._available_local_agents()
            planned, plan_error = await self._plan_multi_agent_payload(
                goal=goal,
                available_agents=available,
                explicit_specs=explicit_pairs,
                preferred_agents=[str(a) for a in preferred_agents if isinstance(a, str)],
            )
            if plan_error:
                await self._reply_logged(update, plan_error, parse_mode=ParseMode.HTML)
                return

            pending_payload = self._set_pending_multi_plan(session_id, planned)
            preview_payload_obj = pending_payload.get("plan_payload")
            preview_payload = (
                preview_payload_obj if isinstance(preview_payload_obj, dict) else {}
            )
            preview_warnings_obj = pending_payload.get("warnings")
            preview_warnings = (
                preview_warnings_obj if isinstance(preview_warnings_obj, list) else []
            )
            preview = self._render_multi_plan_preview(
                goal=str(pending_payload.get("goal") or ""),
                workers=list(pending_payload.get("workers") or []),
                plan_payload=preview_payload,
                warnings=[str(item) for item in preview_warnings],
                include_confirm_hint=not bool(self.config.local_agent_multi_auto_continue),
            )
            await self._reply_logged(update, preview, parse_mode=ParseMode.HTML)

            if self.config.local_agent_multi_auto_continue:
                await self._reply_logged(
                    update,
                    "Auto-continue is enabled. Executing multi-agent run now...",
                )
                await self._execute_pending_multi_plan(update, session_id)
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

            async def _delegation_progress_update(text: str):
                try:
                    await progress.edit_text(text)
                except Exception:
                    pass

            result_text = await self._run_local_agent_task(
                session_id,
                direct_agent,
                task,
                progress_cb=_delegation_progress_update,
            )
            request_entry = (
                "[delegation-request]\n"
                "mode: single\n"
                f"agent: {direct_agent}\n"
                f"task: {task}"
            )
            self.memory.ingest("user", request_entry, session_id)
            memory_entry = self._build_single_delegation_memory_entry(
                agent=direct_agent,
                task=task,
                result_text=result_text,
            )
            self.memory.ingest("assistant", memory_entry, session_id)
            if not self._llm_backoff_active():
                asyncio.create_task(self.maybe_summarize(session_id))
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
                    "(or claude).",
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

            async def _delegation_progress_update(text: str):
                try:
                    await progress.edit_text(text)
                except Exception:
                    pass

            result_text = await self._run_local_agent_task(
                session_id,
                agent,
                task,
                progress_cb=_delegation_progress_update,
            )
            request_entry = (
                "[delegation-request]\n"
                "mode: single\n"
                f"agent: {agent}\n"
                f"task: {task}"
            )
            self.memory.ingest("user", request_entry, session_id)
            memory_entry = self._build_single_delegation_memory_entry(
                agent=agent,
                task=task,
                result_text=result_text,
            )
            self.memory.ingest("assistant", memory_entry, session_id)
            if not self._llm_backoff_active():
                asyncio.create_task(self.maybe_summarize(session_id))
            await self._send_response(progress, update, result_text)
            return

        await self._reply_logged(
            update,
            "Unknown /agent subcommand.\n\n" + self._agent_usage_text(),
            parse_mode=ParseMode.HTML,
        )

    async def _execute_pending_multi_plan(self, update: Update, session_id: str):
        pending = self._get_pending_multi_plan(session_id)
        if not pending:
            await self._reply_logged(
                update,
                "No pending multi-agent plan.\nStart one with <code>/agent multi &lt;goal&gt;</code>.",
                parse_mode=ParseMode.HTML,
            )
            return

        goal = str(pending.get("goal") or "").strip()
        workers_obj = pending.get("workers")
        workers = workers_obj if isinstance(workers_obj, list) else []
        plan_payload_obj = pending.get("plan_payload")
        plan_payload = plan_payload_obj if isinstance(plan_payload_obj, dict) else {}

        resolved_workers: list[tuple[str, str]] = []
        for item in workers:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            label = str(item[0]).strip()
            agent = str(item[1]).strip()
            if not label or not agent:
                continue
            resolved_workers.append((label, agent))

        if not goal or len(resolved_workers) < 2 or not plan_payload:
            self._clear_pending_multi_plan(session_id)
            await self._reply_logged(
                update,
                "Pending multi-agent plan is invalid or expired. Please create a new one.",
            )
            return

        self._clear_pending_multi_plan(session_id)
        await self._execute_multi_agent_plan(
            update=update,
            session_id=session_id,
            goal=goal,
            workers=resolved_workers,
            plan_payload=plan_payload,
        )

    async def _execute_multi_agent_plan(
        self,
        update: Update,
        session_id: str,
        goal: str,
        workers: list[tuple[str, str]],
        plan_payload: dict[str, object],
    ):
        multi_workspace = await asyncio.to_thread(self._create_task_workspace, goal)
        multi_workspace_label = self._workspace_rel_label(multi_workspace)
        agents_path = await asyncio.to_thread(
            self._write_agents_plan_file, multi_workspace, plan_payload
        )
        loaded_payload = await asyncio.to_thread(
            self._load_agents_plan_file, multi_workspace
        )
        if loaded_payload:
            plan_payload = loaded_payload

        handoff_dir = multi_workspace / "handoff"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        before_multi = await asyncio.to_thread(
            self._snapshot_workspace_state, multi_workspace
        )

        plan_lines = ["🤖 <b>Multi-Agent Execution</b>", ""]
        plan_lines.append(f"<b>Goal:</b> {_escape_html(goal)}")
        plan_lines.append(
            f"<b>Task workspace:</b> <code>{_escape_html(multi_workspace_label)}</code>"
        )
        plan_lines.append(f"<b>AGENTS.md:</b> <code>{_escape_html(agents_path.name)}</code>")
        plan_lines.append("")
        plan_lines.append("Starting dependency-phased parallel workers...")
        await self._reply_logged(update, "\n".join(plan_lines), parse_mode=ParseMode.HTML)

        worker_msgs = []
        for index, (label, agent) in enumerate(workers):
            tag = self._multi_agent_tag(label, agent, index)
            worker_msg = await self._reply_logged(
                update,
                f"<code>{_escape_html(tag)}</code>\nQueued...",
                parse_mode=ParseMode.HTML,
            )
            worker_msgs.append(worker_msg)

        def _is_success_result(worker_result: str) -> bool:
            return "✅ Finished in " in (worker_result or "")

        worker_contracts = plan_payload.get("workers")
        contract_list = worker_contracts if isinstance(worker_contracts, list) else []
        workers_by_label = {label: agent for label, agent in workers}
        worker_contract_by_label: dict[str, dict[str, object]] = {
            label: {} for label, _ in workers
        }
        for contract in contract_list:
            if not isinstance(contract, dict):
                continue
            label = str(contract.get("label") or "").strip()
            if not label or label not in workers_by_label:
                continue
            worker_contract_by_label[label] = contract

        dependency_map: dict[str, list[str]] = {}
        unknown_dependency_map: dict[str, list[str]] = {}
        for label, _ in workers:
            contract = worker_contract_by_label.get(label, {})
            depends_obj = contract.get("depends_on")
            raw_deps = (
                [str(dep).strip() for dep in depends_obj]
                if isinstance(depends_obj, list)
                else []
            )
            valid_deps: list[str] = []
            unknown_deps: list[str] = []
            seen: set[str] = set()
            for dep in raw_deps:
                if not dep or dep == label or dep in seen:
                    continue
                seen.add(dep)
                if dep in workers_by_label:
                    valid_deps.append(dep)
                else:
                    unknown_deps.append(dep)
            dependency_map[label] = valid_deps
            unknown_dependency_map[label] = unknown_deps
            contract["depends_on"] = valid_deps

        async def _run_worker(index: int, label: str, agent: str, progress_msg):
            tag = self._multi_agent_tag(label, agent, index)
            worker_contract = worker_contract_by_label.get(label, {})
            worker_task = self._build_multi_agent_worker_task(
                label=label,
                goal=goal,
                workers=workers,
                worker_plan=worker_contract,
                task_workspace_label=multi_workspace_label,
            )

            async def _worker_progress_update(text: str):
                try:
                    await progress_msg.edit_text(f"{tag}\n{text}")
                except Exception:
                    pass

            try:
                result = await self._run_local_agent_task(
                    session_id=session_id,
                    agent=agent,
                    task=worker_task,
                    progress_cb=_worker_progress_update,
                    include_workspace_delta=False,
                    workspace_dir=multi_workspace,
                )
                ok = _is_success_result(result)
                status_text = "✅ Worker completed." if ok else "⚠️ Worker finished with issues."
                try:
                    await progress_msg.edit_text(f"{tag}\n{status_text}")
                except Exception:
                    pass
                return (label, agent, result, ok)
            except Exception as e:
                fail_result = f"⚠️ Worker failed: {e}"
                try:
                    await progress_msg.edit_text(f"{tag}\n{fail_result}")
                except Exception:
                    pass
                return (label, agent, fail_result, False)

        pending = set(workers_by_label.keys())
        completed_ok: set[str] = set()
        failed: set[str] = set()
        results_by_label: dict[str, object] = {}
        index_by_label = {label: idx for idx, (label, _) in enumerate(workers)}
        wait_status_by_label: dict[str, str] = {}

        async def _set_worker_status(label: str, status_text: str):
            if wait_status_by_label.get(label) == status_text:
                return
            wait_status_by_label[label] = status_text
            msg = worker_msgs[index_by_label[label]]
            tag = self._multi_agent_tag(
                label,
                workers_by_label[label],
                index_by_label[label],
            )
            try:
                await msg.edit_text(f"{tag}\n{status_text}")
            except Exception:
                pass

        for label in list(pending):
            unknown_deps = unknown_dependency_map.get(label) or []
            if not unknown_deps:
                continue
            pending.discard(label)
            failed.add(label)
            reason = ", ".join(unknown_deps)
            skip_text = f"⚠️ Skipped because AGENTS.md references unknown dependency: {reason}"
            results_by_label[label] = skip_text
            await _set_worker_status(label, skip_text)

        while pending:
            blocked_now: list[str] = []
            for label in list(pending):
                dep_list = dependency_map.get(label) or []
                if any(dep in failed for dep in dep_list):
                    blocked_now.append(label)

            for label in blocked_now:
                pending.discard(label)
                failed.add(label)
                dep_list = dependency_map.get(label) or []
                reason = ", ".join(d for d in dep_list if d in failed) or "failed dependency"
                skip_text = f"⚠️ Skipped because dependency failed: {reason}"
                results_by_label[label] = skip_text
                await _set_worker_status(label, skip_text)

            ready: list[str] = []
            for label in list(pending):
                dep_list = dependency_map.get(label) or []
                unresolved = [dep for dep in dep_list if dep not in completed_ok]
                if not unresolved:
                    ready.append(label)
                    continue
                resolved_count = len(dep_list) - len(unresolved)
                wait_text = (
                    f"⏳ Waiting for dependencies ({resolved_count}/{len(dep_list)} ready): "
                    + ", ".join(unresolved)
                )
                await _set_worker_status(label, wait_text)

            if not ready:
                for label in list(pending):
                    pending.discard(label)
                    failed.add(label)
                    skip_text = "⚠️ Skipped due to unresolved dependency cycle in AGENTS.md."
                    results_by_label[label] = skip_text
                    await _set_worker_status(label, skip_text)
                break

            run_tasks = [
                _run_worker(
                    index_by_label[label],
                    label,
                    workers_by_label[label],
                    worker_msgs[index_by_label[label]],
                )
                for label in ready
            ]
            phase_results = await asyncio.gather(*run_tasks)

            for label in ready:
                pending.discard(label)

            for result in phase_results:
                label, _agent, worker_result, ok = result
                results_by_label[label] = worker_result
                if ok:
                    completed_ok.add(label)
                else:
                    failed.add(label)

        after_multi = await asyncio.to_thread(
            self._snapshot_workspace_state, multi_workspace
        )
        multi_delta = self._summarize_workspace_delta(before_multi, after_multi)

        final_lines = [
            "🤖 Multi-agent run finished.",
            f"Goal: {goal}",
            f"Task workspace: {multi_workspace_label}",
            "",
        ]

        for index, (label, agent) in enumerate(workers):
            tag = self._multi_agent_tag(label, agent, index)
            worker_result = str(results_by_label.get(label, "⚠️ Worker did not produce output."))
            final_lines.append(f"{tag}")
            final_lines.append(worker_result)
            final_lines.append("")

        final_lines.append(multi_delta)

        request_entry = (
            "[delegation-request]\n"
            "mode: multi\n"
            f"goal: {goal}\n"
            f"workers: {', '.join(f'{label}={agent}' for label, agent in workers)}"
        )
        self.memory.ingest("user", request_entry, session_id)
        memory_entry = self._build_multi_delegation_memory_entry(
            goal=goal,
            workspace_label=multi_workspace_label,
            workers=workers,
            results_by_label=results_by_label,
        )
        self.memory.ingest("assistant", memory_entry, session_id)
        if not self._llm_backoff_active():
            asyncio.create_task(self.maybe_summarize(session_id))

        await self._send_response(None, update, "\n".join(final_lines).strip())

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

    # ── /cron ─────────────────────────────────────────────────

    @staticmethod
    def _cron_usage_text() -> str:
        return (
            "<b>Usage</b>\n"
            "<code>/cron list</code> - list jobs for this chat\n"
            "<code>/cron add every &lt;minutes&gt; &lt;message&gt;</code> - recurring job\n"
            "<code>/cron add at &lt;YYYY-MM-DD HH:MM|timestamp&gt; &lt;message&gt;</code> - one-time job\n"
            "<code>/cron remove &lt;id&gt;</code> - delete a job"
        )

    def _cron_jobs_path(self) -> Path:
        runtime_root = runtime_root_from_workspace(self.config.workspace_path)
        return runtime_root / "cron" / "jobs.json"

    @staticmethod
    def _format_local_datetime(ts: float) -> str:
        value = max(0, int(ts))
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))

    @staticmethod
    def _parse_cron_at(value: str) -> float | None:
        raw = (value or "").strip()
        if not raw:
            return None

        if raw.isdigit():
            parsed = float(raw)
            return parsed if parsed > 0 else None

        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt).timestamp()
            except ValueError:
                continue

        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return None

    def _read_cron_store(self) -> dict[str, Any]:
        path = self._cron_jobs_path()
        if not path.exists():
            return {"jobs": []}

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"Failed to read cron jobs store, resetting to empty: {e}")
            return {"jobs": []}

        raw_jobs = data.get("jobs") if isinstance(data, dict) else []
        if not isinstance(raw_jobs, list):
            return {"jobs": []}

        jobs: list[dict[str, Any]] = []
        for raw in raw_jobs:
            if not isinstance(raw, dict):
                continue

            job_id = str(raw.get("id") or "").strip()
            chat_id = str(raw.get("chat_id") or "").strip()
            mode = str(raw.get("mode") or "").strip().lower()
            text = str(raw.get("text") or "").strip()
            if not job_id or not chat_id or not text or mode not in {"every", "at"}:
                continue

            try:
                next_run_at = float(raw.get("next_run_at"))
            except Exception:
                continue
            if next_run_at <= 0:
                continue

            try:
                created_at = float(raw.get("created_at", time.time()))
            except Exception:
                created_at = time.time()

            job: dict[str, Any] = {
                "id": job_id,
                "chat_id": chat_id,
                "mode": mode,
                "text": text,
                "next_run_at": next_run_at,
                "created_at": created_at,
            }

            if mode == "every":
                try:
                    interval_sec = max(60, int(raw.get("interval_sec", 60)))
                except Exception:
                    continue
                job["interval_sec"] = interval_sec

            jobs.append(job)

        return {"jobs": jobs}

    def _write_cron_store(self, store: dict[str, Any]) -> None:
        payload = {"jobs": store.get("jobs", []) if isinstance(store, dict) else []}
        _atomic_write_json(self._cron_jobs_path(), payload)

    async def _ensure_cron_task(self, bot):
        if self._cron_task and not self._cron_task.done():
            return
        if not hasattr(bot, "send_message"):
            return
        self._cron_task = asyncio.create_task(self._cron_loop(bot))

    async def _cron_loop(self, bot):
        try:
            while True:
                await asyncio.sleep(max(15, int(self._cron_poll_sec)))
                await self._run_due_cron_jobs(bot)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"Cron scheduler stopped due to error: {e}")
        finally:
            self._cron_task = None

    async def _run_due_cron_jobs(self, bot):
        if not hasattr(bot, "send_message"):
            return

        now = time.time()
        changed = False

        async with self._cron_lock:
            store = self._read_cron_store()
            jobs = list(store.get("jobs", []))
            if not jobs:
                return

            updated_jobs: list[dict[str, Any]] = []

            for job in jobs:
                next_run_at = float(job.get("next_run_at", 0))
                if next_run_at <= 0 or next_run_at > now:
                    updated_jobs.append(job)
                    continue

                chat_id_raw = str(job.get("chat_id") or "").strip()
                message_text = str(job.get("text") or "").strip()
                if not chat_id_raw or not message_text:
                    changed = True
                    continue

                try:
                    chat_id = int(chat_id_raw)
                except ValueError:
                    # Ignore malformed chat ids.
                    changed = True
                    continue

                message = f"⏰ Cron reminder\n\n{message_text}"
                html = markdown_to_telegram_html(message)

                async def _send_message(text: str, parse_mode: str | None = None):
                    return await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)

                sent = await self._try_send(_send_message, html)
                if not sent:
                    updated_jobs.append(job)
                    continue

                self._cron_last_run_at = now
                mode = str(job.get("mode") or "").strip().lower()
                if mode == "every":
                    interval_sec = max(60, int(job.get("interval_sec", 60)))
                    job["next_run_at"] = now + interval_sec
                    updated_jobs.append(job)
                    changed = True
                else:
                    # one-time "at" job: remove after successful run
                    changed = True

            if not changed and len(updated_jobs) != len(jobs):
                changed = True

            if changed:
                self._write_cron_store({"jobs": updated_jobs})

    def _render_cron_list(self, session_id: str) -> str:
        store = self._read_cron_store()
        jobs = [j for j in store.get("jobs", []) if str(j.get("chat_id")) == session_id]
        jobs.sort(key=lambda job: float(job.get("next_run_at", 0)))

        lines = ["⏰ <b>Cron Jobs</b>", ""]
        if not jobs:
            lines.append("No cron jobs for this chat.")
            lines.append("")
            lines.append(self._cron_usage_text())
            return "\n".join(lines)

        now = time.time()
        for job in jobs:
            job_id = str(job.get("id") or "")
            mode = str(job.get("mode") or "")
            text = str(job.get("text") or "")
            next_run = float(job.get("next_run_at", 0))
            when = _escape_html(self._format_local_datetime(next_run))
            in_hint = self._format_elapsed(max(0, next_run - now))

            if mode == "every":
                interval_min = max(1, int(job.get("interval_sec", 60)) // 60)
                lines.append(
                    f"• <code>{_escape_html(job_id)}</code> every <code>{interval_min}m</code> "
                    f"(next: <code>{when}</code>, in {in_hint})"
                )
            else:
                lines.append(
                    f"• <code>{_escape_html(job_id)}</code> at <code>{when}</code> (in {in_hint})"
                )
            lines.append(f"  {_escape_html(text)}")

        lines.append("")
        lines.append(self._cron_usage_text())
        return "\n".join(lines)

    async def cmd_cron(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        session_id = self._session_id_from_update(update)
        args = context.args or []
        self._log_user_message(session_id, f"/cron {' '.join(args)}".strip())

        if hasattr(context.bot, "send_message"):
            await self._ensure_cron_task(context.bot)

        sub = (args[0].strip().lower() if args else "list")

        if sub in {"list", "ls", "show", "status"}:
            async with self._cron_lock:
                text = self._render_cron_list(session_id)
            await self._reply_logged(update, text, parse_mode=ParseMode.HTML)
            return

        if sub in {"add", "create"}:
            if len(args) < 4:
                await self._reply_logged(
                    update,
                    "Usage:\n"
                    "<code>/cron add every &lt;minutes&gt; &lt;message&gt;</code>\n"
                    "<code>/cron add at &lt;YYYY-MM-DD HH:MM|timestamp&gt; &lt;message&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            mode = args[1].strip().lower()
            now = time.time()
            job: dict[str, Any] | None = None
            schedule_desc = ""

            if mode == "every":
                try:
                    interval_min = max(1, int(args[2]))
                except ValueError:
                    await self._reply_logged(
                        update,
                        "Minutes must be a number.\n"
                        "Usage: <code>/cron add every &lt;minutes&gt; &lt;message&gt;</code>",
                        parse_mode=ParseMode.HTML,
                    )
                    return

                text = " ".join(args[3:]).strip()
                if not text:
                    await self._reply_logged(
                        update,
                        "Message is required.\n"
                        "Usage: <code>/cron add every &lt;minutes&gt; &lt;message&gt;</code>",
                        parse_mode=ParseMode.HTML,
                    )
                    return

                interval_sec = interval_min * 60
                job = {
                    "id": uuid.uuid4().hex[:8],
                    "chat_id": session_id,
                    "mode": "every",
                    "interval_sec": interval_sec,
                    "next_run_at": now + interval_sec,
                    "text": text,
                    "created_at": now,
                }
                schedule_desc = f"every <code>{interval_min}m</code>"

            elif mode == "at":
                if len(args) < 4:
                    await self._reply_logged(
                        update,
                        "Usage: <code>/cron add at &lt;YYYY-MM-DD HH:MM|timestamp&gt; &lt;message&gt;</code>",
                        parse_mode=ParseMode.HTML,
                    )
                    return

                run_at: float | None = None
                text_start_idx = 3

                # First try single-token datetime forms.
                run_at = self._parse_cron_at(args[2])
                if run_at is None and len(args) >= 5:
                    # Then try split "YYYY-MM-DD HH:MM".
                    run_at = self._parse_cron_at(f"{args[2]} {args[3]}")
                    text_start_idx = 4

                text = " ".join(args[text_start_idx:]).strip()
                if run_at is None or not text:
                    await self._reply_logged(
                        update,
                        "Usage: <code>/cron add at &lt;YYYY-MM-DD HH:MM|timestamp&gt; &lt;message&gt;</code>",
                        parse_mode=ParseMode.HTML,
                    )
                    return

                if run_at <= now:
                    await self._reply_logged(
                        update,
                        "The scheduled time must be in the future.",
                    )
                    return

                job = {
                    "id": uuid.uuid4().hex[:8],
                    "chat_id": session_id,
                    "mode": "at",
                    "next_run_at": run_at,
                    "text": text,
                    "created_at": now,
                }
                schedule_desc = f"at <code>{_escape_html(self._format_local_datetime(run_at))}</code>"
            else:
                await self._reply_logged(
                    update,
                    "Supported modes: <code>every</code>, <code>at</code>.\n\n"
                    + self._cron_usage_text(),
                    parse_mode=ParseMode.HTML,
                )
                return

            assert job is not None
            async with self._cron_lock:
                store = self._read_cron_store()
                jobs = list(store.get("jobs", []))
                jobs.append(job)
                self._write_cron_store({"jobs": jobs})

            jobs_path = self._cron_jobs_path()
            await self._reply_logged(
                update,
                "\n".join(
                    [
                        f"⏰ Cron job added: <code>{_escape_html(str(job['id']))}</code>",
                        f"Schedule: {schedule_desc}",
                        f"Store: <code>{_escape_html(jobs_path.as_posix())}</code>",
                    ]
                ),
                parse_mode=ParseMode.HTML,
            )
            return

        if sub in {"remove", "rm", "delete", "del"}:
            if len(args) < 2:
                await self._reply_logged(
                    update,
                    "Usage: <code>/cron remove &lt;id&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            target_id = args[1].strip()
            if not target_id:
                await self._reply_logged(
                    update,
                    "Usage: <code>/cron remove &lt;id&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            async with self._cron_lock:
                store = self._read_cron_store()
                jobs = list(store.get("jobs", []))
                updated = [
                    job
                    for job in jobs
                    if not (
                        str(job.get("id")) == target_id
                        and str(job.get("chat_id")) == session_id
                    )
                ]

                if len(updated) == len(jobs):
                    removed = False
                else:
                    removed = True
                    self._write_cron_store({"jobs": updated})

            if removed:
                await self._reply_logged(
                    update,
                    f"Removed cron job <code>{_escape_html(target_id)}</code>.",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await self._reply_logged(
                    update,
                    f"No cron job found for id <code>{_escape_html(target_id)}</code> in this chat.",
                    parse_mode=ParseMode.HTML,
                )
            return

        await self._reply_logged(
            update,
            "Unknown /cron subcommand.\n\n" + self._cron_usage_text(),
            parse_mode=ParseMode.HTML,
        )
