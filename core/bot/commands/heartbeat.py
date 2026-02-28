"""Heartbeat scheduler helpers and /heartbeat command handlers."""

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

class CommandsHeartbeatMixin:
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

