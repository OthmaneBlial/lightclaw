from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config import Config
from core.bot import LightClawBot


def _payload(goal):
    return {
        "goal": goal,
        "workers": [("builder", "codex"), ("reviewer", "codex")],
        "plan_payload": {"workers": [{"label": "builder", "owned_paths": ["src/"]}]},
    }


def _bot():
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = Config(telegram_allowed_users=["42"])
    bot._session_id_from_update = lambda update: str(update.effective_chat.id)
    bot._privileged_rate_limited = lambda *_args, **_kwargs: False
    bot._log_user_message = Mock()
    bot._available_local_agents = lambda: {"codex": "/fixture/codex"}
    bot._pending_multi_plan_by_session = {}
    bot._pending_multi_plan_ttl_sec = 900
    bot._pending_wipe_confirm = {}
    bot._pending_trusted_agent_run_by_session = {}
    bot._pending_voice_goal_by_session = {}
    bot._voice_request_ids_by_session = {}
    bot._session_summaries = {}
    bot._summary_key = lambda session_id: session_id
    bot._invalidate_active_message_requests = Mock()
    bot._invalidate_session_summary = Mock()
    bot.memory = SimpleNamespace(clear_session=Mock())
    bot._reply_logged = AsyncMock()
    bot._render_multi_plan_preview = Mock(side_effect=lambda **kwargs: kwargs["goal"])
    bot._execute_multi_agent_plan = AsyncMock()
    return bot


def _update(chat_id=456):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=chat_id, type="private"),
        message=SimpleNamespace(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("older_error", [False, True])
@pytest.mark.parametrize("replacement", ["new", "clear", "cancel", "global-clear"])
async def test_late_planner_cannot_replace_newer_request_or_restore_cleared_plan(
    replacement, older_error
):
    bot = _bot()
    started = asyncio.Event()
    release = asyncio.Event()

    async def plan(**kwargs):
        goal = kwargs["goal"]
        if goal == "older goal":
            started.set()
            await release.wait()
            if older_error:
                return {}, "obsolete planner error"
        return _payload(goal), ""

    bot._plan_multi_agent_payload = plan
    update = _update()
    older = asyncio.create_task(
        bot.cmd_agent(update, SimpleNamespace(args=["multi", "older goal"]))
    )
    await started.wait()
    if replacement == "new":
        await bot.cmd_agent(update, SimpleNamespace(args=["multi", "latest goal"]))
        latest = bot._get_pending_multi_plan("456")
    elif replacement == "clear":
        await bot.cmd_clear(update, SimpleNamespace(args=[]))
    elif replacement == "cancel":
        await bot.cmd_agent(update, SimpleNamespace(args=["multi", "cancel"]))
    else:
        bot._clear_pending_actions()
    replies_before = bot._reply_logged.await_count
    release.set()
    await older

    assert bot._reply_logged.await_count == replies_before
    if replacement == "new":
        assert bot._get_pending_multi_plan("456") is latest
        assert latest["goal"] == "latest goal"
    else:
        assert bot._get_pending_multi_plan("456") is None
    bot._execute_multi_agent_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_plan_edit_cannot_replace_new_request_in_the_same_chat():
    bot = _bot()
    bot._set_pending_multi_plan("456", bot._decorate_pending_plan(_payload("original goal")))
    started = asyncio.Event()
    release = asyncio.Event()

    async def plan(**kwargs):
        if kwargs.get("feedback"):
            started.set()
            await release.wait()
        return _payload(kwargs["goal"]), ""

    bot._plan_multi_agent_payload = plan
    update = _update()
    editing = asyncio.create_task(
        bot.cmd_agent(update, SimpleNamespace(args=["multi", "edit", "change scope"]))
    )
    await started.wait()
    assert bot._get_pending_multi_plan("456") is None
    await bot.cmd_agent(update, SimpleNamespace(args=["multi", "latest goal"]))
    latest = bot._get_pending_multi_plan("456")
    release.set()
    await editing

    assert bot._get_pending_multi_plan("456") is latest
    assert latest["goal"] == "latest goal"
    assert bot._reply_logged.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("replace", [False, True])
async def test_interrupted_planning_clears_only_its_own_request(failure, replace):
    bot = _bot()
    started = asyncio.Event()
    release = asyncio.Event()

    async def plan(**kwargs):
        if kwargs["goal"] == "interrupted goal":
            started.set()
            await release.wait()
            raise failure()
        return _payload(kwargs["goal"]), ""

    bot._plan_multi_agent_payload = plan
    update = _update()
    interrupted = asyncio.create_task(
        bot.cmd_agent(update, SimpleNamespace(args=["multi", "interrupted goal"]))
    )
    await started.wait()
    if replace:
        await bot.cmd_agent(update, SimpleNamespace(args=["multi", "latest goal"]))
        latest = bot._get_pending_multi_plan("456")
    release.set()
    with pytest.raises(failure):
        await interrupted
    if replace:
        assert bot._get_pending_multi_plan("456") is latest
    else:
        assert not bot._pending_multi_plan_by_session


@pytest.mark.asyncio
async def test_planning_in_one_chat_does_not_invalidate_another_chat():
    bot = _bot()
    started = asyncio.Event()
    release = asyncio.Event()

    async def plan(**kwargs):
        if kwargs["goal"] == "slow goal":
            started.set()
            await release.wait()
        return _payload(kwargs["goal"]), ""

    bot._plan_multi_agent_payload = plan
    slow = asyncio.create_task(
        bot.cmd_agent(_update(456), SimpleNamespace(args=["multi", "slow goal"]))
    )
    await started.wait()
    await bot.cmd_agent(_update(789), SimpleNamespace(args=["multi", "other goal"]))
    release.set()
    await slow

    assert bot._get_pending_multi_plan("456")["goal"] == "slow goal"
    assert bot._get_pending_multi_plan("789")["goal"] == "other goal"


async def _plan_action(bot, update, approval_id, action):
    if action == "slash":
        await bot.cmd_agent(update, SimpleNamespace(args=["multi", "confirm"]))
    elif action == "text":
        await bot._process_user_message(update, SimpleNamespace(), "yes")
    else:
        update.callback_query = SimpleNamespace(
            data=f"lc:plan:{action}:{approval_id}", answer=AsyncMock(), message=update.message
        )
        update.effective_message = update.message
        await bot.handle_run_action(update, SimpleNamespace())


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["slash", "text", "approve", "confirm-risk"])
@pytest.mark.parametrize("high_risk", [False, True])
async def test_incomplete_review_cannot_execute_or_prime_second_confirmation(action, high_risk):
    bot = _bot()
    bot._active_run_tasks_by_session = {}
    bot._active_run_ids_by_session = {}
    pending = bot._set_pending_multi_plan("456", bot._decorate_pending_plan(_payload("update docs")))
    pending["review"]["second_confirmation_required"] = high_risk
    entered = asyncio.Event()
    release = asyncio.Event()
    sends = 0

    async def send(*_args, **_kwargs):
        nonlocal sends
        sends += 1
        if sends == 2:
            entered.set()
            await release.wait()

    bot._reply_logged.side_effect = send
    update = _update()
    preview = asyncio.create_task(
        bot._reply_multi_plan_preview(update, "scope " * 850, pending["approval_id"], False)
    )
    await entered.wait()
    await _plan_action(bot, update, pending["approval_id"], action)
    bot._execute_multi_agent_plan.assert_not_awaited()
    assert not pending["review"]["second_confirmation_prompted"]
    assert not pending["review"]["second_confirmed"]
    assert bot._get_pending_multi_plan("456") is pending

    release.set()
    await preview
    assert pending["review_delivered"]
    await _plan_action(bot, update, pending["approval_id"], "slash")
    if high_risk:
        bot._execute_multi_agent_plan.assert_not_awaited()
        assert pending["review"]["second_confirmation_prompted"]
        await _plan_action(bot, update, pending["approval_id"], "confirm-risk")
    bot._execute_multi_agent_plan.assert_awaited_once()
    assert not bot._pending_multi_plan_by_session


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("replace", [False, True])
async def test_failed_review_delivery_invalidates_only_its_own_plan(failure, replace):
    bot = _bot()
    pending = bot._set_pending_multi_plan("456", bot._decorate_pending_plan(_payload("old goal")))
    entered = asyncio.Event()
    release = asyncio.Event()
    first = True

    async def send(*_args, **_kwargs):
        nonlocal first
        if first:
            first = False
            entered.set()
            await release.wait()
            raise failure()

    bot._reply_logged.side_effect = send
    update = _update()
    preview = asyncio.create_task(
        bot._reply_multi_plan_preview(update, "old review", pending["approval_id"], False)
    )
    await entered.wait()
    if replace:
        bot._plan_multi_agent_payload = AsyncMock(return_value=(_payload("latest goal"), ""))
        await bot.cmd_agent(update, SimpleNamespace(args=["multi", "latest goal"]))
        latest = bot._get_pending_multi_plan("456")
    release.set()
    with pytest.raises(failure):
        await preview
    if replace:
        assert bot._get_pending_multi_plan("456") is latest
        assert latest["review_delivered"]
    else:
        assert not bot._pending_multi_plan_by_session
    bot._execute_multi_agent_plan.assert_not_awaited()
