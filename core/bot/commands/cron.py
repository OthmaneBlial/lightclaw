"""Cron scheduler helpers and /cron command handlers."""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import ContextTypes

from ...fs import FileTooLargeError, read_json_object
from ...fs import atomic_write_json as _atomic_write_json
from ...logging_setup import log
from ...markdown import _escape_html, markdown_to_telegram_html
from ...personality import runtime_root_from_workspace
from ..messaging import _TelegramHTMLChunker

MAX_CRON_STORE_BYTES = 1024 * 1024


class CronStoreReadError(RuntimeError):
    """Cron state cannot be read safely."""


class CommandsCronMixin:
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
    def _is_renderable_cron_timestamp(value: float) -> bool:
        try:
            return value > 0 and math.isfinite(value) and bool(time.localtime(value))
        except (OverflowError, OSError, ValueError):
            return False


    @staticmethod
    def _parse_cron_at(value: str) -> float | None:
        raw = (value or "").strip()
        if not raw:
            return None

        try:
            parsed = float(raw) if raw.isdigit() else datetime.fromisoformat(raw).timestamp()
        except (OverflowError, OSError, ValueError):
            return None

        return parsed if CommandsCronMixin._is_renderable_cron_timestamp(parsed) else None


    def _read_cron_store(self) -> dict[str, Any]:
        path = self._cron_jobs_path()
        try:
            data = read_json_object(path, default={"jobs": []}, max_bytes=MAX_CRON_STORE_BYTES)
        except Exception as e:
            raise CronStoreReadError(f"Failed to read cron jobs store: {e}") from e

        raw_jobs = data.get("jobs")
        if not isinstance(raw_jobs, list):
            raise CronStoreReadError("Cron jobs store must contain a jobs list")

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
            if not self._is_renderable_cron_timestamp(next_run_at):
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
                try:
                    next_run = time.time() + interval_sec
                except OverflowError:
                    continue
                if not self._is_renderable_cron_timestamp(next_run):
                    continue
                job["interval_sec"] = interval_sec

            jobs.append(job)

        return {"jobs": jobs}


    def _write_cron_store(self, store: dict[str, Any]) -> None:
        payload = {"jobs": store.get("jobs", []) if isinstance(store, dict) else []}
        _atomic_write_json(self._cron_jobs_path(), payload, max_bytes=MAX_CRON_STORE_BYTES)


    async def _reply_cron_store_read_error(self, update: Update, error: CronStoreReadError) -> None:
        log.warning(str(error))
        await self._reply_logged(
            update,
            "Cron jobs are unavailable because storage is unreadable. No changes made.",
        )


    async def _ensure_cron_task(self, bot):
        if self._cron_task and not self._cron_task.done():
            return
        if not hasattr(bot, "send_message"):
            return
        self._cron_task = self._create_background_task(self._cron_loop(bot))


    async def _cron_loop(self, bot):
        try:
            while True:
                await asyncio.sleep(max(15, int(self._cron_poll_sec)))
                try:
                    await self._run_due_cron_jobs(bot)
                except Exception as e:
                    log.error(f"Cron scheduler iteration failed; retrying: {e}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"Cron scheduler stopped due to error: {e}")
        finally:
            self._cron_task = None


    async def _run_due_cron_jobs(self, bot):
        if not hasattr(bot, "send_message"):
            return

        async with self._cron_iteration_lock:
            now = time.time()
            updates: dict[str, dict[str, Any] | None] = {}
            async with self._cron_lock:
                jobs = list(self._read_cron_store().get("jobs", []))

            for job in jobs:
                next_run_at = float(job.get("next_run_at", 0))
                if next_run_at <= 0 or next_run_at > now:
                    continue

                job_id = str(job["id"])
                chat_id_raw = str(job.get("chat_id") or "").strip()
                message_text = str(job.get("text") or "").strip()
                if not chat_id_raw or not message_text:
                    updates[job_id] = None
                    continue

                try:
                    chat_id = int(chat_id_raw)
                except ValueError:
                    updates[job_id] = None
                    continue

                # Persisted reminders must obey the policy of this bot startup.
                if chat_id > 0:
                    if not self.is_allowed(chat_id):
                        continue
                elif not (
                    chat_id < 0
                    and self.config.telegram_public_bot_ack
                    and not self.config.telegram_allowed_users
                ):
                    continue

                message = f"⏰ Cron reminder\n\n{message_text}"
                html = markdown_to_telegram_html(message)

                async def _send_message(
                    text: str,
                    parse_mode: str | None = None,
                    target_chat_id=chat_id,
                ):
                    return await bot.send_message(
                        chat_id=target_chat_id,
                        text=text,
                        parse_mode=parse_mode,
                    )

                mode = str(job.get("mode") or "").strip().lower()
                retry_delay = max(60, int(self._cron_poll_sec))
                try:
                    sent = await self._try_send(_send_message, html)
                except RetryAfter as e:
                    retry_after = e.retry_after
                    if isinstance(retry_after, timedelta):
                        retry_after = retry_after.total_seconds()
                    retry_delay = max(retry_delay, math.ceil(retry_after))
                    sent = False
                    log.warning("Cron job %s rate limited; retrying later", job_id)
                except TelegramError as e:
                    sent = False
                    log.warning("Cron job %s delivery failed; retrying later: %s", job_id, e)

                if not sent:
                    if mode == "every":
                        retry_delay = max(retry_delay, int(job.get("interval_sec", 60)))
                    job["next_run_at"] = now + retry_delay
                    updates[job_id] = job
                elif mode == "every":
                    interval_sec = max(60, int(job.get("interval_sec", 60)))
                    job["next_run_at"] = now + interval_sec
                    updates[job_id] = job
                    self._cron_last_run_at = now
                else:
                    # one-time "at" job: remove after successful run
                    updates[job_id] = None
                    self._cron_last_run_at = now

            if updates:
                async with self._cron_lock:
                    current_jobs = list(self._read_cron_store().get("jobs", []))
                    merged_jobs = [
                        updated
                        for job in current_jobs
                        if (updated := updates.get(str(job["id"]), job)) is not None
                    ]
                    self._write_cron_store({"jobs": merged_jobs})


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
        try:
            await self._cmd_cron(update, context)
        except CronStoreReadError as e:
            await self._reply_cron_store_read_error(update, e)
        except FileTooLargeError:
            await self._reply_logged(
                update, "Cron storage is full. No changes made; remove existing reminders to free space."
            )


    async def _cmd_cron(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_update_allowed(update):
            return
        if self._privileged_rate_limited(update.effective_user.id, "cron", limit=10):
            await self._reply_logged(
                update,
                "⚠️ Too many privileged scheduler requests. Retry in about one minute.",
            )
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
            chunks = _TelegramHTMLChunker(max_len=3000)
            chunks.feed(text)
            for chunk in chunks.finish():
                await self._reply_logged(update, chunk, parse_mode=ParseMode.HTML)
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
                try:
                    next_run_at = now + interval_sec
                except OverflowError:
                    next_run_at = math.inf
                if not self._is_renderable_cron_timestamp(next_run_at):
                    await self._reply_logged(update, "That interval is too far in the future.")
                    return

                job = {
                    "id": uuid.uuid4().hex[:8],
                    "chat_id": session_id,
                    "mode": "every",
                    "interval_sec": interval_sec,
                    "next_run_at": next_run_at,
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

                split_run_at = self._parse_cron_at(f"{args[2]} {args[3]}") if len(args) >= 5 else None
                run_at = split_run_at or self._parse_cron_at(args[2])
                if split_run_at is not None:
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
