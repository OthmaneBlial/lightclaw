from __future__ import annotations

import asyncio
import multiprocessing
import os
import re
import threading
import time
from datetime import datetime
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter

from config import Config
from core.bot import LightClawBot
from core.bot.base import BotBaseMixin
from core.bot.commands import cron as cron_commands
from core.bot.commands.cron import CommandsCronMixin
from core.bot.messaging import BotMessagingMixin


class CronHarness(CommandsCronMixin, BotMessagingMixin, BotBaseMixin):
    def __init__(self):
        self.config = Config(telegram_allowed_users=["123"])


def _append_cron_job_in_process(store_path, job_id, barrier):
    bot = CronHarness()
    bot._cron_jobs_path = lambda: Path(store_path)
    barrier.wait(timeout=20)

    def append_job(jobs):
        time.sleep(0.05)
        jobs.append(
            {
                "id": job_id,
                "chat_id": "123",
                "mode": "at",
                "next_run_at": time.time() + 60,
                "created_at": time.time(),
                "text": job_id,
            }
        )
        return True, None

    bot._mutate_cron_store(append_job)


def test_forum_topic_session_ids_and_memory_scope():
    bot = BotBaseMixin.__new__(BotBaseMixin)
    def message_update(chat_id, chat_type, topic_id=None):
        message = {
            "message_id": 7,
            "date": 1,
            "chat": {"id": chat_id, "type": chat_type},
            "text": "fixture",
        }
        if topic_id is not None:
            message["message_thread_id"] = topic_id
        return Update.de_json({"update_id": 1, "message": message}, None)

    topic_update = message_update(-100, "supergroup", 84)
    general_update = message_update(-100, "supergroup", 1)
    private_topic_update = message_update(42, "private", 84)
    private_update = message_update(42, "private")
    callback_update = Update.de_json(
        {
            "update_id": 2,
            "callback_query": {
                "id": "query-1",
                "from": {"id": 42, "is_bot": False, "first_name": "Test"},
                "chat_instance": "group-instance",
                "message": {
                    "message_id": 8,
                    "date": 1,
                    "chat": {"id": -100, "type": "supergroup"},
                    "message_thread_id": 84,
                    "text": "Approve?",
                },
                "data": "run:approve",
            },
        },
        None,
    )

    assert bot._session_id_from_update(topic_update) == "-100:topic:84"
    assert bot._session_id_from_update(general_update) == "-100:topic:1"
    assert bot._session_id_from_update(private_topic_update) == "42:topic:84"
    assert bot._session_id_from_update(private_update) == "42"
    assert bot._session_id_from_update(callback_update) == "-100:topic:84"
    assert bot._memory_recall_current_session_only(topic_update)
    assert bot._memory_recall_current_session_only(private_topic_update)
    assert not bot._memory_recall_current_session_only(private_update)
    assert bot._telegram_target_from_session_id("-100") == (-100, None)
    assert bot._telegram_target_from_session_id("-100:topic:84") == (-100, 84)
    assert bot._telegram_target_from_session_id("-100:topic:1") == (-100, 1)
    assert bot._telegram_target_from_session_id("terminal-session") is None


@pytest.mark.asyncio
async def test_cron_list_delivers_all_jobs_in_bounded_messages(tmp_path):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = Config(telegram_allowed_users=["123"])
    bot._cron_lock = asyncio.Lock()
    bot._privileged_request_times = {}
    bot._cron_jobs_path = lambda: tmp_path / "jobs.json"
    bot._session_id_from_update = lambda _update: "123"
    bot._log_user_message = Mock()
    bot._log_bot_message = Mock()
    texts = [f"Reminder {i}: " + "🐾<&" * 600 for i in range(2)]
    bot._write_cron_store({"jobs": [
        {
            "id": f"job-{i}", "chat_id": "123", "mode": "at",
            "text": text, "next_run_at": time.time() + 60,
        }
        for i, text in enumerate(texts)
    ]})
    delivered = []

    async def reply_text(text, parse_mode=None, **_kwargs):
        assert parse_mode == ParseMode.HTML
        plain = unescape(re.sub(r"<[^>]+>", "", text))
        if len(plain.encode("utf-16-le")) // 2 > 4096:
            raise BadRequest("Message is too long")
        delivered.append(plain)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=123, type="private"),
        message=SimpleNamespace(reply_text=reply_text),
    )

    await bot.cmd_cron(update, SimpleNamespace(args=["list"], bot=SimpleNamespace()))

    assert len(delivered) > 1
    assert all(len(text.encode("utf-16-le")) // 2 <= 3000 for text in delivered)
    result = "".join(delivered)
    for i, text in enumerate(texts):
        assert result.count(f"job-{i}") == 1
        assert text in result
    assert "/cron remove <id>" in result


@pytest.mark.asyncio
async def test_cron_store_reads_and_writes_run_off_event_loop(tmp_path, monkeypatch):
    loop_thread = threading.get_ident()
    read_threads = []
    write_threads = []
    jobs_path = tmp_path / "cron" / "jobs.json"
    read_store = cron_commands.read_json_object
    write_store = cron_commands._atomic_write_json

    def record_read_thread(*args, **kwargs):
        read_threads.append(threading.get_ident())
        return read_store(*args, **kwargs)

    def record_write_thread(*args, **kwargs):
        write_threads.append(threading.get_ident())
        return write_store(*args, **kwargs)

    monkeypatch.setattr(cron_commands, "read_json_object", record_read_thread)
    monkeypatch.setattr(cron_commands, "_atomic_write_json", record_write_thread)
    bot = CronHarness()
    bot._cron_lock = asyncio.Lock()
    bot._cron_iteration_lock = asyncio.Lock()
    bot._cron_poll_sec = 15
    bot._cron_last_run_at = 0
    bot._cron_jobs_path = lambda: jobs_path
    bot.is_update_allowed = lambda _update: True
    bot.is_allowed = lambda _chat_id: True
    bot._privileged_rate_limited = lambda *_args, **_kwargs: False
    bot._session_id_from_update = lambda _update: "123"
    bot._log_user_message = Mock()
    bot._reply_logged = AsyncMock()
    bot._try_send = AsyncMock(return_value=True)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123), message=SimpleNamespace()
    )

    await bot.cmd_cron(
        update, SimpleNamespace(args=["add", "every", "1", "Check releases"], bot=SimpleNamespace())
    )

    write_store(
        jobs_path,
        {
            "jobs": [{
                "id": "due",
                "chat_id": "123",
                "mode": "at",
                "text": "Check the release",
                "next_run_at": time.time() - 1,
            }]
        },
        max_bytes=cron_commands.MAX_CRON_STORE_BYTES,
    )
    await bot._run_due_cron_jobs(SimpleNamespace(send_message=AsyncMock()))

    assert len(read_threads) >= 2
    assert len(write_threads) >= 2
    assert all(thread_id != loop_thread for thread_id in read_threads + write_threads)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["at", "every"])
@pytest.mark.parametrize(
    ("allowed_users", "public_ack", "chat_id", "should_send"),
    [
        (["123"], False, "123", True),
        (["456"], False, "123", False),
        ([], False, "123", False),
        ([], True, "123", True),
        ([], True, "-123", True),
        ([], False, "-123", False),
        (["123"], True, "-123", False),
        (["456"], True, "123", False),
    ],
)
async def test_saved_cron_jobs_respect_current_access_policy(
    tmp_path, mode, allowed_users, public_ack, chat_id, should_send
):
    bot = CronHarness()
    bot._cron_lock = asyncio.Lock()
    bot._cron_iteration_lock = asyncio.Lock()
    bot._cron_poll_sec = 15
    bot._cron_last_run_at = 0
    bot._cron_jobs_path = lambda: tmp_path / "jobs.json"
    bot._write_cron_store({"jobs": [{
        "id": "saved", "chat_id": chat_id, "mode": mode, "interval_sec": 60,
        "text": "Private reminder", "next_run_at": time.time() - 1,
    }]})
    original = bot._cron_jobs_path().read_bytes()
    bot.config = Config(
        telegram_allowed_users=allowed_users, telegram_public_bot_ack=public_ack
    )
    telegram_bot = SimpleNamespace(send_message=AsyncMock())

    await bot._run_due_cron_jobs(telegram_bot)

    if should_send:
        telegram_bot.send_message.assert_awaited_once()
        assert telegram_bot.send_message.await_args.kwargs["chat_id"] == int(chat_id)
    else:
        telegram_bot.send_message.assert_not_awaited()
        assert bot._cron_jobs_path().read_bytes() == original
        assert bot._cron_last_run_at == 0
        bot.config = Config(telegram_public_bot_ack=True)
        await bot._run_due_cron_jobs(telegram_bot)
        telegram_bot.send_message.assert_awaited_once()

    remaining = bot._read_cron_store()["jobs"]
    if mode == "at":
        assert remaining == []
    else:
        assert remaining[0]["next_run_at"] > time.time()


@pytest.mark.asyncio
async def test_saved_cron_topic_job_is_delivered_to_its_forum_topic(tmp_path):
    bot = CronHarness()
    bot.config = Config(telegram_public_bot_ack=True)
    bot._cron_lock = asyncio.Lock()
    bot._cron_iteration_lock = asyncio.Lock()
    bot._cron_poll_sec = 15
    bot._cron_last_run_at = 0
    bot._cron_jobs_path = lambda: tmp_path / "jobs.json"
    bot._write_cron_store({"jobs": [{
        "id": "topic-reminder",
        "chat_id": "-100:topic:84",
        "mode": "at",
        "text": "Review the topic",
        "next_run_at": time.time() - 1,
    }]})
    telegram_bot = SimpleNamespace(send_message=AsyncMock())

    await bot._run_due_cron_jobs(telegram_bot)

    kwargs = telegram_bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == -100
    assert kwargs["message_thread_id"] == 84


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "minimum_delay", "retry_after"),
    [
        pytest.param(NetworkError, 60, None, id="network"),
        pytest.param(RetryAfter, 120, None, id="duration"),
        pytest.param(RetryAfter, 120, 120, id="integer"),
    ],
)
async def test_failed_cron_delivery_is_retained_with_retry_delay(
    error_type, minimum_delay, retry_after, monkeypatch
):
    error = error_type("timed out" if error_type is NetworkError else 120)
    if retry_after is not None:
        monkeypatch.setattr(
            RetryAfter,
            "retry_after",
            property(lambda _self: retry_after),
        )
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
    bot._read_cron_store = lambda: {"jobs": [dict(job) for job in jobs]}

    def mutate_store(mutation):
        current_jobs = [dict(job) for job in jobs]
        changed, result = mutation(current_jobs)
        if changed:
            jobs[:] = current_jobs
            writes.append({"jobs": list(current_jobs)})
        return result

    bot._mutate_cron_store = mutate_store
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


@pytest.mark.asyncio
async def test_cron_rejects_missing_dst_time_and_accepts_explicit_offsets():
    original_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Europe/Paris"
        time.tzset()

        assert CommandsCronMixin._parse_cron_at("2026-03-29 02:30") is None
        first = CommandsCronMixin._parse_cron_at("2026-10-25 02:30")
        second = CommandsCronMixin._parse_cron_at("2026-10-25T02:30+01:00")

        assert first is not None and second is not None
        assert time.strftime("%z", time.localtime(first)) == "+0200"
        assert second - first == 3600

        bot = CronHarness()
        bot.is_update_allowed = lambda _update: True
        bot._privileged_rate_limited = lambda *_args, **_kwargs: False
        bot._session_id_from_update = lambda _update: "123"
        bot._log_user_message = lambda *_args: None
        bot._reply_logged = AsyncMock()
        bot._mutate_cron_store = Mock()
        await bot.cmd_cron(
            SimpleNamespace(effective_user=SimpleNamespace(id=1), message=object()),
            SimpleNamespace(
                args=["add", "at", "2026-03-29", "02:30", "Review"],
                bot=SimpleNamespace(),
            ),
        )
        bot._mutate_cron_store.assert_not_called()
        assert "invalid date/time" in bot._reply_logged.await_args.args[1].lower()
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


def test_cron_times_show_the_machine_local_utc_offset():
    timestamp = time.time() + 3600
    rendered = CommandsCronMixin._format_local_datetime(timestamp)
    offset = time.strftime("%z", time.localtime(timestamp))
    usage = CommandsCronMixin._cron_usage_text()

    assert re.fullmatch(r"[+-]\d{4}", offset)
    assert rendered.endswith(f" {offset}")
    assert "/cron add at YYYY-MM-DD HH:MM" in usage
    assert "one-time, local time" in usage
    assert "/cron add at &lt;timestamp&gt;" in usage
    assert "YYYY-MM-DDTHH:MM+02:00" in usage


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
async def test_split_cron_datetime_uses_time_as_schedule_not_message(tmp_path):
    bot = CronHarness()
    bot._cron_lock = asyncio.Lock()
    bot.is_update_allowed = lambda _update: True
    bot._privileged_rate_limited = lambda *_args, **_kwargs: False
    bot._session_id_from_update = lambda _update: "123"
    bot._log_user_message = lambda *_args: None
    bot._reply_logged = AsyncMock()
    bot._cron_jobs_path = lambda: tmp_path / "jobs.json"
    writes = []

    def mutate_store(mutation):
        jobs = []
        changed, result = mutation(jobs)
        if changed:
            writes.append({"jobs": jobs})
        return result

    bot._mutate_cron_store = mutate_store
    scheduled = datetime.fromtimestamp(time.time() + 7200).replace(
        second=0, microsecond=0
    )
    context = SimpleNamespace(
        args=[
            "add",
            "at",
            scheduled.strftime("%Y-%m-%d"),
            scheduled.strftime("%H:%M"),
            "Review",
            "the patch",
        ],
        bot=SimpleNamespace(),
    )

    await bot.cmd_cron(
        SimpleNamespace(
            effective_user=SimpleNamespace(id=1), message=object()
        ),
        context,
    )

    assert writes[0]["jobs"][0]["text"] == "Review the patch"
    assert writes[0]["jobs"][0]["next_run_at"] == scheduled.timestamp()


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
    bot._mutate_cron_store = Mock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=object())
    context = SimpleNamespace(args=["add", "every", "9" * 100, "check"], bot=SimpleNamespace())

    await bot.cmd_cron(update, context)

    bot._mutate_cron_store.assert_not_called()
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
async def test_cron_add_preserves_reminders_when_store_would_exceed_read_limit(tmp_path):
    bot = CronHarness()
    bot._cron_lock = asyncio.Lock()
    bot.is_update_allowed = lambda _update: True
    bot._privileged_rate_limited = lambda *_args, **_kwargs: False
    bot._session_id_from_update = lambda _update: "123"
    bot._log_user_message = Mock()
    bot._reply_logged = AsyncMock()
    store_path = tmp_path / "jobs.json"
    bot._cron_jobs_path = lambda: store_path
    bot._write_cron_store({"jobs": [{
        "id": "existing", "chat_id": "123", "mode": "at",
        "text": "x" * (1024 * 1024 - 512),
        "next_run_at": time.time() + 60, "created_at": time.time(),
    }]})
    original = store_path.read_bytes()
    assert bot._read_cron_store()["jobs"][0]["id"] == "existing"
    update = SimpleNamespace(effective_user=SimpleNamespace(id=123), message=object())
    context = SimpleNamespace(args=["add", "every", "5", "🐾" * 80], bot=SimpleNamespace())

    await bot.cmd_cron(update, context)

    assert store_path.stat().st_size == len(original)
    assert store_path.read_bytes() == original
    assert [job["id"] for job in bot._read_cron_store()["jobs"]] == ["existing"]
    assert "full" in bot._reply_logged.await_args.args[1].lower()
    assert "no changes made" in bot._reply_logged.await_args.args[1].lower()
    await bot.cmd_cron(update, SimpleNamespace(args=["remove", "existing"], bot=SimpleNamespace()))
    await bot.cmd_cron(update, context)
    assert [job["text"] for job in bot._read_cron_store()["jobs"]] == ["🐾" * 80]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing_session_id", "current_session_id", "at_limit"),
    [
        ("123", "123", True),
        ("999", "123", False),
        ("-100:topic:84", "-100:topic:84", True),
        ("-100:topic:84", "-100:topic:42", False),
    ],
)
async def test_cron_add_caps_reminders_per_session_without_affecting_other_sessions(
    tmp_path, existing_session_id, current_session_id, at_limit
):
    bot = CronHarness()
    bot._cron_lock = asyncio.Lock()
    bot.is_update_allowed = lambda _update: True
    bot._privileged_rate_limited = lambda *_args, **_kwargs: False
    bot._session_id_from_update = lambda _update: current_session_id
    bot._log_user_message = Mock()
    bot._reply_logged = AsyncMock()
    store_path = tmp_path / "jobs.json"
    bot._cron_jobs_path = lambda: store_path
    bot._write_cron_store({"jobs": [
        {
            "id": f"existing-{index}",
            "chat_id": existing_session_id,
            "mode": "every",
            "interval_sec": 60,
            "text": "existing",
            "next_run_at": time.time() + 60,
        }
        for index in range(10)
    ]})
    update = SimpleNamespace(effective_user=SimpleNamespace(id=123), message=object())

    await bot.cmd_cron(
        update,
        SimpleNamespace(args=["add", "every", "5", "new reminder"], bot=SimpleNamespace()),
    )

    jobs = bot._read_cron_store()["jobs"]
    assert len(jobs) == (10 if at_limit else 11)
    if at_limit:
        assert all(job["text"] == "existing" for job in jobs)
        assert "session already has 10 reminders" in bot._reply_logged.await_args.args[1]
    else:
        assert jobs[-1]["text"] == "new reminder"


def test_cron_store_mutations_are_serialized_across_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    store_path = tmp_path / "jobs.json"
    processes = [
        context.Process(
            target=_append_cron_job_in_process,
            args=(str(store_path), f"worker-{worker}", barrier),
        )
        for worker in range(4)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)
        assert not any(process.is_alive() for process in processes)
        assert [process.exitcode for process in processes] == [0] * len(processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    bot = CronHarness()
    bot._cron_jobs_path = lambda: store_path
    jobs = bot._read_cron_store()["jobs"]
    assert {job["id"] for job in jobs} == {f"worker-{worker}" for worker in range(4)}


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
