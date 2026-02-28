"""/skills command handlers and rendering helpers."""

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

class CommandsSkillsMixin:
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

