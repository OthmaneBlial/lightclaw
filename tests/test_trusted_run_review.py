from __future__ import annotations

import asyncio
from html import unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config import Config
from core.bot import LightClawBot


def _bot():
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = Config(telegram_allowed_users=["42"])
    bot._session_id_from_update = lambda _update: "review"
    bot._privileged_rate_limited = lambda *_args, **_kwargs: False
    bot._log_user_message = Mock()
    bot._pending_trusted_agent_run_by_session = {}
    bot._reply_logged = AsyncMock()
    bot._execute_one_shot_delegation = AsyncMock()
    return bot


def _update():
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=456, type="private"),
        message=SimpleNamespace(),
    )


async def _request(bot, task):
    await bot.cmd_agent(_update(), SimpleNamespace(args=["trusted", "codex", task]))
    return bot._pending_trusted_agent_run_by_session["review"]


async def _click(bot, action):
    update = _update()
    update.callback_query = SimpleNamespace(
        data=action, answer=AsyncMock(), message=update.message
    )
    update.effective_message = update.message
    await bot.handle_run_action(update, SimpleNamespace())


@pytest.mark.asyncio
async def test_trusted_buttons_approve_only_the_complete_visible_task_once():
    bot = _bot()
    task = "<draft> & inspect\u202efile\x00 " + "🐾" * 3500
    pending = await _request(bot, task)
    replies = bot._reply_logged.await_args_list
    assert len(replies) > 1
    visible = "".join(unescape(bot._strip_html_for_log(call.args[1])) for call in replies)
    assert "codex" in visible
    assert pending["task"] in visible
    assert pending["task"] == bot._visible_review_text(task)
    for call in replies:
        assert len(unescape(bot._strip_html_for_log(call.args[1])).encode("utf-16-le")) <= 6000
    assert all(call.kwargs.get("reply_markup") is None for call in replies[:-1])
    keyboard = replies[-1].kwargs["reply_markup"].inline_keyboard[0]
    assert [button.text for button in keyboard] == ["Approve host run", "Discard"]
    assert keyboard[0].callback_data == f"lc:trusted:approve:{pending['approval_id']}"

    await _click(bot, keyboard[0].callback_data)
    await _click(bot, keyboard[0].callback_data)

    bot._execute_one_shot_delegation.assert_awaited_once()
    assert bot._execute_one_shot_delegation.await_args.kwargs == {
        "session_id": "review",
        "agent": "codex",
        "task": pending["task"],
        "capability_profile": "trusted-command",
    }
    assert not bot._pending_trusted_agent_run_by_session


@pytest.mark.asyncio
async def test_trusted_stale_button_and_bare_confirm_cannot_approve_replacement():
    bot = _bot()
    old = await _request(bot, "inspect old files")
    latest = await _request(bot, "inspect new files")

    await bot.cmd_agent(_update(), SimpleNamespace(args=["trusted", "confirm"]))
    await _click(bot, f"lc:trusted:approve:{old['approval_id']}")
    bot._execute_one_shot_delegation.assert_not_awaited()
    assert bot._pending_trusted_agent_run_by_session["review"] is latest

    await _click(bot, f"lc:trusted:deny:{latest['approval_id']}")
    bot._execute_one_shot_delegation.assert_not_awaited()
    assert not bot._pending_trusted_agent_run_by_session


@pytest.mark.asyncio
@pytest.mark.parametrize("review_id", ["é" * 16, "x", "", "0" * 16])
async def test_invalid_trusted_review_id_cannot_consume_current_request(review_id):
    bot = _bot()
    pending = await _request(bot, "inspect files")
    await bot.cmd_agent(_update(), SimpleNamespace(args=["trusted", "confirm", review_id]))
    bot._execute_one_shot_delegation.assert_not_awaited()
    assert bot._pending_trusted_agent_run_by_session["review"] is pending


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError, None])
@pytest.mark.parametrize("replace", [False, True])
async def test_incomplete_trusted_review_cannot_run_or_clear_a_newer_review(failure, replace):
    bot = _bot()
    entered = asyncio.Event()
    release = asyncio.Event()
    first = True

    async def send(*_args, **_kwargs):
        nonlocal first
        if first:
            first = False
            entered.set()
            await release.wait()
            if failure:
                raise failure()

    bot._reply_logged.side_effect = send
    request = asyncio.create_task(_request(bot, "old task"))
    await entered.wait()
    old = bot._pending_trusted_agent_run_by_session["review"]
    await bot.cmd_agent(
        _update(), SimpleNamespace(args=["trusted", "confirm", old["approval_id"]])
    )
    bot._execute_one_shot_delegation.assert_not_awaited()
    latest = await _request(bot, "new task") if replace else None
    release.set()
    if failure:
        with pytest.raises(failure):
            await request
    else:
        await request
    if replace:
        assert bot._pending_trusted_agent_run_by_session["review"] is latest
        assert latest["reviewed"]
    elif failure:
        assert not bot._pending_trusted_agent_run_by_session
    else:
        assert bot._pending_trusted_agent_run_by_session["review"] is old
        assert old["reviewed"]
