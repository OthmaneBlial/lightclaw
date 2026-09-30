from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config import Config
from core.bot import LightClawBot
from memory import MemoryStore


@pytest.mark.asyncio
async def test_clear_during_summary_does_not_restore_old_context():
    started = asyncio.Event()
    finish = asyncio.Event()

    async def summarize(*args, **kwargs):
        started.set()
        await finish.wait()
        return "summary from erased conversation"

    bot = LightClawBot.__new__(LightClawBot)
    bot.config = Config(telegram_allowed_users=["42"], context_window=128)
    bot.memory = SimpleNamespace(
        get_recent=Mock(
            return_value=[
                {"role": "user", "content": f"old detail {index}"}
                for index in range(21)
            ]
        ),
        scope_for=Mock(return_value=("telegram-user:42", "/workspace")),
        get_summary=Mock(return_value=""),
        set_summary=Mock(),
        clear_session=Mock(),
    )
    bot.llm = SimpleNamespace(chat=AsyncMock(side_effect=summarize))
    bot._llm_backoff_until = 0.0
    bot._summarizing = set()
    bot._session_summaries = {}
    bot._summary_generation_by_session = {}
    bot._pending_wipe_confirm = {}
    bot._pending_multi_plan_by_session = {}
    bot._pending_trusted_agent_run_by_session = {}
    bot._pending_voice_goal_by_session = {}
    bot._voice_request_ids_by_session = {}
    bot._session_id_from_update = Mock(return_value="chat-42")
    bot._log_user_message = Mock()
    bot._privileged_rate_limited = Mock(return_value=False)
    bot._reply_logged = AsyncMock()

    task = asyncio.create_task(bot.maybe_summarize("chat-42"))
    await started.wait()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42, type="private"),
        message=SimpleNamespace(),
    )
    try:
        await bot.cmd_clear(update, SimpleNamespace())
    finally:
        finish.set()
    await task

    assert ("chat-42", "telegram-user:42", "/workspace") not in bot._session_summaries
    assert bot._summary_generation_by_session == {}
    bot.memory.clear_session.assert_called_once_with("chat-42")


@pytest.mark.asyncio
async def test_summary_reads_and_persists_sqlite_summary_across_restart(tmp_path):
    session_id = "chat-42"
    memory = MemoryStore(tmp_path / "memory.db")
    memory.bind_session(
        session_id,
        user_namespace="telegram-user:42",
        workspace_namespace=str(tmp_path),
    )
    memory.set_summary(session_id, "Earlier choice: use the local SQLite store.")
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(context_window=256)
    bot.memory = memory
    bot.llm = SimpleNamespace(chat=AsyncMock(return_value="Updated durable summary."))
    bot._session_summaries = {}
    bot._summary_generation_by_session = {}
    bot._clear_llm_backoff = Mock()

    history = [
        {"role": "user", "content": f"recent detail {index}"}
        for index in range(6)
    ]
    try:
        await bot._summarize_session(session_id, history, 0)

        prompt = bot.llm.chat.await_args.args[0][0]["content"]
        assert "Earlier choice: use the local SQLite store." in prompt
        assert memory.get_summary(session_id) == "Updated durable summary."

        memory.db.close()
        memory = MemoryStore(tmp_path / "memory.db")
        memory.bind_session(
            session_id,
            user_namespace="telegram-user:42",
            workspace_namespace=str(tmp_path),
        )
        restarted_bot = LightClawBot.__new__(LightClawBot)
        restarted_bot.memory = memory
        restarted_bot._session_summaries = {}
        assert restarted_bot._get_session_summary(session_id) == "Updated durable summary."
    finally:
        memory.db.close()


def test_summary_cache_isolated_by_user_within_shared_group(tmp_path):
    session_id = "group-1"
    memory = MemoryStore(tmp_path / "memory.db")
    memory.bind_session(
        session_id, user_namespace="telegram-user:42", workspace_namespace=str(tmp_path)
    )
    memory.set_summary(session_id, "private summary for user 42")
    bot = LightClawBot.__new__(LightClawBot)
    bot.memory = memory
    bot._session_summaries = {}
    bot._summary_generation_by_session = {}
    bot._summarizing = {bot._summary_key(session_id)}

    try:
        assert bot._get_session_summary(session_id) == "private summary for user 42"
        memory.bind_session(
            session_id, user_namespace="telegram-user:99", workspace_namespace=str(tmp_path)
        )
        key_99 = bot._summary_key(session_id)
        assert key_99 not in bot._summarizing
        assert bot._get_session_summary(session_id) == ""
        bot._summarizing.add(key_99)
        bot._invalidate_session_summary(session_id)
        assert bot._summary_generation_by_session[key_99] == 1

        memory.bind_session(
            session_id, user_namespace="telegram-user:42", workspace_namespace=str(tmp_path)
        )
        assert bot._get_session_summary(session_id) == "private summary for user 42"
        assert bot._summary_generation_by_session.get(bot._summary_key(session_id), 0) == 0
    finally:
        memory.db.close()


def test_summary_cache_evicts_least_recent_user_scope(tmp_path, monkeypatch):
    monkeypatch.setattr("core.bot.context.MAX_CACHED_SESSION_SUMMARIES", 2)
    session_id = "group-1"
    memory = MemoryStore(tmp_path / "memory.db")
    bot = LightClawBot.__new__(LightClawBot)
    bot.memory = memory
    bot._session_summaries = {}

    def store_summary(user_id, value):
        memory.bind_session(
            session_id, user_namespace=f"telegram-user:{user_id}", workspace_namespace=str(tmp_path)
        )
        memory.set_summary(session_id, value)
        return bot._get_session_summary(session_id)

    try:
        assert store_summary(42, "summary A") == "summary A"
        key_42 = bot._summary_key(session_id)
        assert store_summary(99, "summary B") == "summary B"
        key_99 = bot._summary_key(session_id)
        memory.bind_session(
            session_id, user_namespace="telegram-user:42", workspace_namespace=str(tmp_path)
        )
        assert bot._get_session_summary(session_id) == "summary A"
        assert store_summary(17, "summary C") == "summary C"
        key_17 = bot._summary_key(session_id)
        assert set(bot._session_summaries) == {key_42, key_17}

        memory.bind_session(
            session_id, user_namespace="telegram-user:99", workspace_namespace=str(tmp_path)
        )
        assert bot._get_session_summary(session_id) == "summary B"
        assert set(bot._session_summaries) == {key_17, key_99}
    finally:
        memory.db.close()


@pytest.mark.asyncio
async def test_show_reports_persisted_summary_after_restart():
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = Config(telegram_allowed_users=["42"])
    bot.memory = SimpleNamespace(
        stats=Mock(return_value={"total_interactions": 0}),
        scope_for=Mock(return_value=("telegram-user:42", "/workspace")),
        get_summary=Mock(return_value="Persisted context."),
    )
    bot.skills = SimpleNamespace(
        list_skills=Mock(return_value=[]),
        active_records=Mock(return_value=[]),
    )
    bot.jobs = SimpleNamespace(diagnostics=Mock(return_value={"counts": {}}))
    bot.start_time = 1.0
    bot._session_summaries = {}
    bot._session_id_from_update = lambda _update: "42"
    bot._log_user_message = Mock()
    bot._agent_mode_by_session = {}
    bot._file_mode_by_session = {}
    bot._pending_multi_plan_by_session = {}
    bot._reply_logged = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(type="private"),
        message=SimpleNamespace(),
    )

    await bot.cmd_show(update, SimpleNamespace())

    assert "<b>Session summary:</b> ✅" in bot._reply_logged.await_args.args[1]


@pytest.mark.asyncio
async def test_confirmed_global_wipe_revokes_pending_actions_across_chats():
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    bot._session_id_from_update = lambda _update: "current"
    bot._log_user_message = Mock()
    bot._pending_wipe_confirm = {
        "current": {
            "user_id": 42,
            "expires_at": 9_999_999_999,
            "expires_monotonic": 9_999_999_999,
        },
        "other": {
            "user_id": 99,
            "expires_at": 9_999_999_999,
            "expires_monotonic": 9_999_999_999,
        },
    }
    bot._pending_multi_plan_by_session = {"current": {}, "other": {}}
    bot._pending_trusted_agent_run_by_session = {"current": {}, "other": {}}
    bot._pending_voice_goal_by_session = {"current": {}, "other": {}}
    bot._voice_request_ids_by_session = {"current": "a", "other": "b"}
    bot._active_run_tasks_by_session = {"other": asyncio.current_task()}
    bot._session_summaries = {"current": "old", "other": "old"}
    bot._invalidate_active_summaries = Mock()
    bot.memory = SimpleNamespace(clear_all=Mock())
    bot._reply_logged = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42, type="private"),
        message=SimpleNamespace(),
    )

    await bot.cmd_wipe_memory(update, SimpleNamespace(args=["confirm"]))

    assert bot._pending_wipe_confirm == {}
    assert bot._pending_multi_plan_by_session == {}
    assert bot._pending_trusted_agent_run_by_session == {}
    assert bot._pending_voice_goal_by_session == {}
    assert bot._voice_request_ids_by_session == {}
    assert bot._active_run_tasks_by_session == {"other": asyncio.current_task()}
    assert bot._session_summaries == {}
    bot.memory.clear_all.assert_called_once()
    assert "Active runs continue" in bot._reply_logged.await_args.args[1]


@pytest.mark.asyncio
async def test_global_wipe_confirmation_cannot_be_completed_by_another_group_user():
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    bot._session_id_from_update = lambda _update: "-group-7"
    bot._log_user_message = Mock()
    bot._pending_wipe_confirm = {
        "-group-7": {
            "user_id": 17,
            "expires_at": 9_999_999_999,
            "expires_monotonic": 9_999_999_999,
        }
    }
    bot.memory = SimpleNamespace(clear_all=Mock())
    bot._reply_logged = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=-7, type="group"),
        message=SimpleNamespace(),
    )

    await bot.cmd_wipe_memory(update, SimpleNamespace(args=["confirm"]))

    bot.memory.clear_all.assert_not_called()
    assert bot._pending_wipe_confirm["-group-7"]["user_id"] == 17
    assert "No active wipe confirmation for your Telegram user" in (
        bot._reply_logged.await_args.args[1]
    )


@pytest.mark.asyncio
async def test_global_wipe_confirmation_uses_monotonic_expiry():
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    bot._session_id_from_update = lambda _update: "-group-7"
    bot._log_user_message = Mock()
    bot._pending_wipe_confirm = {
        "-group-7": {
            "user_id": 42,
            "expires_at": 9_999_999_999,
            "expires_monotonic": 0,
        }
    }
    bot.memory = SimpleNamespace(clear_all=Mock())
    bot._reply_logged = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=-7, type="group"),
        message=SimpleNamespace(),
    )

    await bot.cmd_wipe_memory(update, SimpleNamespace(args=["confirm"]))

    bot.memory.clear_all.assert_not_called()
    assert "No active wipe confirmation" in bot._reply_logged.await_args.args[1]
