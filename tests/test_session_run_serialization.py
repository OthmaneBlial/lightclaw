from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from core.bot import LightClawBot


@pytest.mark.asyncio
async def test_agent_runs_serialize_per_chat_but_not_across_chats():
    bot = LightClawBot.__new__(LightClawBot)
    bot._session_run_locks = {}
    bot._active_run_tasks_by_session = {}
    started: dict[str, asyncio.Event] = {
        "chat-a": asyncio.Event(),
        "chat-b": asyncio.Event(),
    }
    release = asyncio.Event()
    calls: list[str] = []

    async def run_agent(**kwargs):
        session_id = kwargs["session_id"]
        calls.append(session_id)
        started[session_id].set()
        await release.wait()
        return "complete"

    bot._run_local_agent_task_impl = run_agent
    first = asyncio.create_task(
        bot._run_local_agent_task("chat-a", "codex", "first task")
    )
    await started["chat-a"].wait()
    assert bot._active_run_tasks_by_session["chat-a"] is first

    rejected = await bot._run_local_agent_task("chat-a", "claude", "second task")
    assert "already active" in rejected
    assert calls == ["chat-a"]

    other_chat = asyncio.create_task(
        bot._run_local_agent_task("chat-b", "claude", "independent task")
    )
    await started["chat-b"].wait()
    assert calls == ["chat-a", "chat-b"]

    release.set()
    assert await asyncio.gather(first, other_chat) == ["complete", "complete"]
    assert bot._session_run_locks == {}
    assert bot._active_run_tasks_by_session == {}


@pytest.mark.asyncio
async def test_rejected_multi_plan_does_not_replace_active_cancel_task():
    bot = LightClawBot.__new__(LightClawBot)
    bot._session_run_locks = {"chat-a": asyncio.Lock()}
    await bot._session_run_locks["chat-a"].acquire()
    active_task = asyncio.current_task()
    bot._active_run_tasks_by_session = {"chat-a": active_task}
    bot._reply_logged = AsyncMock()

    await bot._execute_pending_multi_plan(object(), "chat-a")

    assert bot._active_run_tasks_by_session["chat-a"] is active_task
    bot._reply_logged.assert_awaited_once()
    bot._session_run_locks["chat-a"].release()
