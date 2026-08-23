from __future__ import annotations

import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.bot import LightClawBot


def test_plan_review_exposes_scope_commands_estimate_and_second_confirmation():
    bot = LightClawBot.__new__(LightClawBot)
    pending = bot._decorate_pending_plan(
        {
            "goal": "Publish a release after deleting the obsolete credential file",
            "plan_payload": {
                "workers": [
                    {
                        "label": "release",
                        "owned_paths": ["docs/release.md"],
                        "acceptance_checks": [
                            {"type": "command_succeeds", "command": "python -m pytest"}
                        ],
                    }
                ]
            },
        }
    )

    review = pending["review"]
    assert review["risk_level"] == "high"
    assert review["changed_paths"] == ["docs/release.md"]
    assert review["proposed_commands"] == ["python -m pytest"]
    assert review["estimated_minutes"] == {"min": 2, "max": 15}
    assert review["second_confirmation_required"] is True
    rendered = bot._render_plan_review(pending)
    assert "docs/release.md" in rendered
    assert "python -m pytest" in rendered


def test_inline_keyboards_cover_required_plan_and_result_actions():
    bot = LightClawBot.__new__(LightClawBot)
    plan_data = [button.callback_data for row in bot._inline_plan_keyboard().inline_keyboard for button in row]
    result_data = [
        button.callback_data
        for row in bot._inline_result_keyboard(["builder"]).inline_keyboard
        for button in row
    ]
    assert {"lc:plan:approve", "lc:plan:edit", "lc:plan:deny", "lc:run:cancel"} <= set(plan_data)
    assert {"lc:run:diff", "lc:run:accept", "lc:run:retry:builder", "lc:run:cancel"} <= set(result_data)


@pytest.mark.asyncio
async def test_voice_transcription_waits_for_explicit_approval(monkeypatch):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(groq_api_key="fixture")
    bot.is_allowed = lambda _user_id: True
    bot._pending_voice_goal_by_session = {}
    bot._reply_logged = AsyncMock()
    bot._process_user_message = AsyncMock()
    monkeypatch.setattr("core.bot.handlers.transcribe_voice", AsyncMock(return_value="Build the fixture"))

    voice_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"audio")))
    voice = SimpleNamespace(get_file=AsyncMock(return_value=voice_file))
    message = SimpleNamespace(voice=voice, caption="", reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        message=message,
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))

    await bot.handle_voice(update, context)

    bot._process_user_message.assert_not_awaited()
    assert bot._pending_voice_goal_by_session["456"]["transcription"] == "Build the fixture"
    call = bot._reply_logged.await_args
    assert "not executed" in call.args[1]
    assert call.kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_high_risk_callback_requires_ordered_second_confirmation():
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_allowed = lambda _user_id: True
    pending = {
        "review": {
            "second_confirmation_required": True,
            "second_confirmation_prompted": False,
            "second_confirmed": False,
        }
    }
    bot._get_pending_multi_plan = lambda _session: pending
    bot._reply_logged = AsyncMock()
    bot._execute_approved_plan_action = AsyncMock()
    query = SimpleNamespace(data="lc:plan:confirm-risk", answer=AsyncMock(), message=SimpleNamespace())
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        effective_message=query.message,
    )
    context = SimpleNamespace()

    await bot.handle_run_action(update, context)
    bot._execute_approved_plan_action.assert_not_awaited()

    query.data = "lc:plan:approve"
    await bot.handle_run_action(update, context)
    assert pending["review"]["second_confirmation_prompted"] is True
    bot._execute_approved_plan_action.assert_not_awaited()

    query.data = "lc:plan:confirm-risk"
    await bot.handle_run_action(update, context)
    bot._execute_approved_plan_action.assert_awaited_once()
    assert pending["review"]["second_confirmed"] is True


@pytest.mark.asyncio
async def test_long_result_is_private_file_artifact_not_chat_wall(tmp_path):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(tmp_path / "workspace"))
    message = SimpleNamespace(reply_document=AsyncMock(), reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=456),
        message=message,
    )

    await bot._send_response(None, update, "evidence\n" * 1000)

    message.reply_document.assert_awaited_once()
    message.reply_text.assert_not_awaited()
    files = list((tmp_path / "workspace" / ".lightclaw-meta" / "messages").glob("*.md"))
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert files[0].read_text(encoding="utf-8").startswith("evidence")
