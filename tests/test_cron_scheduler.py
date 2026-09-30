from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.error import NetworkError, RetryAfter

from core.bot.commands.cron import CommandsCronMixin
from core.bot.messaging import BotMessagingMixin


class CronHarness(CommandsCronMixin, BotMessagingMixin):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "minimum_delay"),
    [(NetworkError("timed out"), 60), (RetryAfter(120), 120)],
)
async def test_failed_cron_delivery_is_retained_with_retry_delay(error, minimum_delay):
    bot = CronHarness()
    now = time.time()
    jobs = [
        {
            "id": "periodic",
            "chat_id": "123",
            "mode": "every",
            "interval_sec": 300,
            "text": "Check the build",
            "next_run_at": now - 1,
        },
        {
            "id": "reminder",
            "chat_id": "123",
            "mode": "at",
            "text": "Review the patch",
            "next_run_at": now - 1,
        },
    ]
    writes = []
    bot._cron_lock = asyncio.Lock()
    bot._cron_iteration_lock = asyncio.Lock()
    bot._cron_poll_sec = 15
    bot._cron_last_run_at = 0
    bot._read_cron_store = lambda: {"jobs": jobs}
    bot._write_cron_store = writes.append
    telegram_bot = SimpleNamespace(send_message=AsyncMock(side_effect=[None, error]))

    await bot._run_due_cron_jobs(telegram_bot)

    assert telegram_bot.send_message.await_count == 2
    assert len(writes) == 1
    stored_jobs = {job["id"]: job for job in writes[0]["jobs"]}
    assert stored_jobs["periodic"]["next_run_at"] >= now + 300
    assert stored_jobs["reminder"]["next_run_at"] >= now + minimum_delay


@pytest.mark.asyncio
async def test_cron_loop_retries_after_transient_iteration_error(monkeypatch):
    bot = CronHarness()
    bot._cron_poll_sec = 15
    bot._cron_task = object()
    attempts = 0

    async def no_wait(_seconds):
        return None

    async def run_due(_telegram_bot):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary store failure")
        raise asyncio.CancelledError

    bot._run_due_cron_jobs = run_due
    monkeypatch.setattr("core.bot.commands.cron.asyncio.sleep", no_wait)

    await bot._cron_loop(SimpleNamespace())

    assert attempts == 2
    assert bot._cron_task is None


def test_cron_parser_rejects_timestamp_outside_localtime_range():
    assert CommandsCronMixin._parse_cron_at("9" * 100) is None


def test_cron_store_discards_unrenderable_timestamps_and_intervals(monkeypatch, tmp_path):
    bot = CronHarness()
    bot._cron_jobs_path = lambda: tmp_path / "jobs.json"
    monkeypatch.setattr(
        "core.bot.commands.cron.read_json_object",
        lambda *_args, **_kwargs: {
            "jobs": [
                {"id": "bad-time", "chat_id": "1", "mode": "at", "text": "x", "next_run_at": 1e100},
                {"id": "bad-interval", "chat_id": "1", "mode": "every", "text": "x", "next_run_at": time.time(), "interval_sec": 10**100},
                {"id": "valid", "chat_id": "1", "mode": "at", "text": "x", "next_run_at": time.time() + 60},
            ]
        },
    )

    assert [job["id"] for job in bot._read_cron_store()["jobs"]] == ["valid"]


@pytest.mark.asyncio
async def test_cron_interval_outside_localtime_range_is_rejected():
    bot = CronHarness()
    bot._cron_lock = asyncio.Lock()
    bot._cron_poll_sec = 15
    bot.is_update_allowed = lambda _update: True
    bot._privileged_rate_limited = lambda *_args, **_kwargs: False
    bot._session_id_from_update = lambda _update: "123"
    bot._log_user_message = lambda *_args: None
    bot._reply_logged = AsyncMock()
    bot._read_cron_store = lambda: {"jobs": []}
    bot._write_cron_store = Mock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=object())
    context = SimpleNamespace(args=["add", "every", "9" * 100, "check"], bot=SimpleNamespace())

    await bot.cmd_cron(update, context)

    bot._write_cron_store.assert_not_called()
    assert "too far in the future" in bot._reply_logged.await_args.args[1].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [["list"], ["add", "every", "5", "new job"], ["remove", "existing"]],
)
@pytest.mark.parametrize("content", ['{"jobs":', '{"jobs":{}}'])
async def test_cron_command_preserves_unreadable_store(tmp_path, args, content):
    bot = CronHarness()
    bot._cron_lock = asyncio.Lock()
    bot.is_update_allowed = lambda _update: True
    bot._privileged_rate_limited = lambda *_args, **_kwargs: False
    bot._session_id_from_update = lambda _update: "123"
    bot._log_user_message = lambda *_args: None
    bot._reply_logged = AsyncMock()
    bot._write_cron_store = Mock()
    store_path = tmp_path / "jobs.json"
    store_path.write_text(content, encoding="utf-8")
    original = store_path.read_bytes()
    bot._cron_jobs_path = lambda: store_path
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=object())
    context = SimpleNamespace(args=args, bot=SimpleNamespace())

    await bot.cmd_cron(update, context)

    bot._write_cron_store.assert_not_called()
    assert store_path.read_bytes() == original
    assert "unreadable" in bot._reply_logged.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_cron_delivery_does_not_block_schedule_edits(tmp_path):
    bot = CronHarness()
    bot._cron_lock = asyncio.Lock()
    bot._cron_iteration_lock = asyncio.Lock()
    bot._cron_poll_sec = 15
    bot._cron_last_run_at = 0
    bot._cron_jobs_path = lambda: tmp_path / "jobs.json"
    bot._write_cron_store(
        {
            "jobs": [
                {
                    "id": "due",
                    "chat_id": "123",
                    "mode": "at",
                    "text": "reminder",
                    "next_run_at": time.time() - 1,
                }
            ]
        }
    )
    send_started = asyncio.Event()
    finish_send = asyncio.Event()

    async def send_message(**_kwargs):
        send_started.set()
        await finish_send.wait()

    bot.is_update_allowed = lambda _update: True
    bot._privileged_rate_limited = lambda *_args, **_kwargs: False
    bot._session_id_from_update = lambda _update: "123"
    bot._log_user_message = lambda *_args: None
    bot._reply_logged = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=object())
    context = SimpleNamespace(args=["add", "every", "5", "new job"], bot=SimpleNamespace())
    scheduler = asyncio.create_task(bot._run_due_cron_jobs(SimpleNamespace(send_message=send_message)))

    await asyncio.wait_for(send_started.wait(), timeout=1)
    try:
        await asyncio.wait_for(bot.cmd_cron(update, context), timeout=1)
    finally:
        finish_send.set()
        await scheduler

    assert [job["text"] for job in bot._read_cron_store()["jobs"]] == ["new job"]
