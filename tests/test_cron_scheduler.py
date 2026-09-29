from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
