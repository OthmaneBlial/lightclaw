"""Cron scheduler helpers and /cron command handlers."""

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

