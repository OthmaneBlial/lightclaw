from __future__ import annotations

import asyncio
import json
import sqlite3
import stat
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import InputFile
from telegram.error import NetworkError

from core.bot import LightClawBot
from core.jobs import JobStore
from core.workspaces import WorkspaceSafetyError


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
                            {
                                "type": "command_succeeds",
                                "command": "python -m pytest",
                                "cwd": "services/api",
                            }
                        ],
                    }
                ]
            },
        }
    )

    review = pending["review"]
    assert review["risk_level"] == "high"
    assert review["changed_paths"] == ["docs/release.md"]
    assert review["proposed_commands"] == [
        "release (cwd: services/api): python -m pytest"
    ]
    assert review["estimated_minutes"] == {"min": 2, "max": 15}
    assert review["second_confirmation_required"] is True
    rendered = bot._render_plan_review(pending)
    assert "docs/release.md" in rendered
    assert "python -m pytest" in rendered
    assert "does not sandbox" in rendered


def test_plan_responsibilities_trigger_second_confirmation():
    bot = LightClawBot.__new__(LightClawBot)

    pending = bot._decorate_pending_plan(
        {
            "goal": "Improve project documentation",
            "plan_payload": {
                "workers": [
                    {
                        "label": "maintainer",
                        "responsibilities": ["Delete old credential files"],
                    }
                ]
            },
        }
    )

    assert pending["review"]["risk_level"] == "high"
    assert pending["review"]["second_confirmation_required"] is True


def test_plan_expected_outputs_are_visible_and_trigger_second_confirmation():
    bot = LightClawBot.__new__(LightClawBot)
    pending = bot._decorate_pending_plan(
        {
            "goal": "Improve project documentation",
            "plan_payload": {
                "workers": [
                    {
                        "label": "writer",
                        "role": "documentation",
                        "responsibilities": ["Update API documentation"],
                        "expected_outputs": ["Publish the production release"],
                        "owned_paths": ["docs/api.md"],
                    }
                ]
            },
        }
    )

    preview = bot._render_multi_plan_preview(
        "Improve project documentation",
        [("writer", "codex")],
        pending["plan_payload"],
        include_confirm_hint=False,
    )

    assert pending["review"]["second_confirmation_required"] is True
    assert "outputs: Publish the production release" in preview


@pytest.mark.parametrize(
    "worker_action",
    [
        "Rotate API keys",
        "Merge release branch",
        "Overwrite user data",
        "Purge user records",
        "Revoke admin access",
    ],
)
def test_high_risk_worker_actions_require_second_confirmation(worker_action: str):
    bot = LightClawBot.__new__(LightClawBot)
    pending = bot._decorate_pending_plan(
        {
            "goal": "Improve documentation",
            "plan_payload": {
                "workers": [
                    {
                        "role": "documentation",
                        "responsibilities": ["Update API documentation"],
                        "expected_outputs": [worker_action],
                    }
                ]
            },
        }
    )

    assert pending["review"]["second_confirmation_required"] is True


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /tmp/workspace",
        "curl -fsS https://example.com",
        "python -c \"import shutil; shutil.rmtree('/tmp/workspace')\"",
        "mv /tmp/important /tmp/old",
        "cp -f /tmp/generated /tmp/important",
        "git restore --source=HEAD --worktree .",
        "git checkout -- .",
        "python -c \"import os; os.replace('/tmp/a', '/tmp/b')\"",
        "python -c \"import shutil; shutil.move('/tmp/a', '/tmp/b')\"",
        "python -c \"from pathlib import Path; Path('/tmp/a').replace('/tmp/b')\"",
        "python -c \"from pathlib import Path; Path('/tmp/a').rename('/tmp/b')\"",
        "cat /etc/config",
        "sed -i 's/old/new/' /tmp/config",
        "perl -i -pe 's/old/new/' /tmp/config",
        "tee -a /tmp/config",
        "chmod -R 777 /tmp/workspace",
        "chown alice /tmp/workspace",
    ],
)
def test_risky_commands_and_system_paths_require_second_confirmation(command: str):
    bot = LightClawBot.__new__(LightClawBot)
    pending = bot._decorate_pending_plan(
        {
            "goal": "Run the local checks",
            "plan_payload": {
                "workers": [
                    {
                        "label": "builder",
                        "acceptance_checks": [
                            {"type": "command_succeeds", "command": command}
                        ],
                    }
                ]
            },
        }
    )

    assert pending["review"]["second_confirmation_required"] is True


def test_switching_git_branch_does_not_require_destructive_confirmation():
    bot = LightClawBot.__new__(LightClawBot)
    pending = bot._decorate_pending_plan(
        {
            "goal": "Run the local checks",
            "plan_payload": {
                "workers": [
                    {
                        "label": "builder",
                        "acceptance_checks": [
                            {"type": "command_succeeds", "command": "git checkout main"}
                        ],
                    }
                ]
            },
        }
    )

    assert pending["review"]["second_confirmation_required"] is False


def test_plan_preview_shows_every_worker_responsibility_and_owned_path():
    bot = LightClawBot.__new__(LightClawBot)
    responsibilities = ["Update the API", "Add regression coverage"]
    owned_paths = ["api/routes.py", "api/models.py", "tests/test_api.py", "docs/api.md"]

    rendered = bot._render_multi_plan_preview(
        goal="Update the sample API",
        workers=[("builder", "codex")],
        plan_payload={
            "workers": [
                {
                    "label": "builder",
                    "role": "implementation",
                    "depends_on": [],
                    "responsibilities": responsibilities,
                    "owned_paths": owned_paths,
                }
            ]
        },
    )

    assert all(item in rendered for item in responsibilities + owned_paths)


@pytest.mark.asyncio
async def test_long_plan_preview_is_chunked_with_approval_on_final_chunk():
    bot = LightClawBot.__new__(LightClawBot)
    bot._reply_logged = AsyncMock()
    update = SimpleNamespace()
    approval_id = "0123456789abcdef"
    preview = bot._render_multi_plan_preview("x" * 3900, [], {})
    preview += "\n\n" + bot._render_plan_review({})

    await bot._reply_multi_plan_preview(update, preview, approval_id, False)

    calls = bot._reply_logged.await_args_list
    assert len(calls) > 1
    assert all(len(call.args[1]) < 4096 for call in calls)
    assert all(call.kwargs["reply_markup"] is None for call in calls[:-1])
    assert calls[-1].kwargs["reply_markup"] is not None


def test_plan_with_unreviewed_commands_cannot_be_approved():
    bot = LightClawBot.__new__(LightClawBot)
    approval_id = "0123456789abcdef"
    commands = [f"python -m pytest tests/test_{index}.py" for index in range(7)]
    pending = bot._decorate_pending_plan(
        {
            "goal": "Run reviewed checks",
            "plan_payload": {
                "workers": [
                    {
                        "label": "builder",
                        "acceptance_checks": [
                            {"type": "command_succeeds", "command": command}
                            for command in commands
                        ],
                    }
                ]
            },
        }
    )
    pending["approval_id"] = approval_id
    review = pending["review"]
    assert review["approval_blocked"] is True
    rendered = bot._render_plan_review(pending)
    assert commands[0] in rendered and commands[5] in rendered
    assert commands[6] not in rendered
    assert "first 6 commands shown" in rendered
    assert "edit to review all" in rendered
    keyboard_data = {
        button.callback_data
        for row in bot._inline_plan_keyboard(
            approval_id,
            approval_blocked=review["approval_blocked"],
        ).inline_keyboard
        for button in row
    }
    assert f"lc:plan:approve:{approval_id}" not in keyboard_data
    assert f"lc:plan:edit:{approval_id}" in keyboard_data

    reviewable = bot._decorate_pending_plan(
        {
            "goal": "Run reviewed checks",
            "plan_payload": {
                "workers": [
                    {
                        "label": "builder",
                        "acceptance_checks": [
                            {"type": "command_succeeds", "command": command}
                            for command in commands[:6]
                        ],
                    }
                ]
            },
        }
    )
    assert reviewable["review"]["approval_blocked"] is False
    assert commands[5] in bot._render_plan_review(reviewable)


def test_pending_plan_expires_when_either_clock_reaches_deadline(monkeypatch):
    clock = {"wall": 1_000.0, "monotonic": 10.0}
    monkeypatch.setattr("core.bot.base.time.time", lambda: clock["wall"])
    monkeypatch.setattr("core.bot.base.time.monotonic", lambda: clock["monotonic"])
    bot = LightClawBot.__new__(LightClawBot)
    bot._pending_multi_plan_by_session = {}
    bot._pending_multi_plan_ttl_sec = 900

    wall_jump_plan = bot._set_pending_multi_plan("wall-jump", {}, ttl_sec=30)
    clock["wall"] = 800.0
    clock["monotonic"] = float(wall_jump_plan["expires_monotonic"]) + 1
    assert bot._get_pending_multi_plan("wall-jump") is None

    clock["wall"] = 2_000.0
    clock["monotonic"] = 100.0
    sleep_plan = bot._set_pending_multi_plan("sleep", {}, ttl_sec=30)
    clock["wall"] = float(sleep_plan["expires_at"]) + 1
    assert bot._get_pending_multi_plan("sleep") is None


@pytest.mark.asyncio
async def test_hidden_plan_command_callback_is_refused():
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    pending = {
        "approval_id": "0123456789abcdef",
        "review": {
            "proposed_commands": ["python -m pytest"],
            "approval_blocked": True,
            "second_confirmation_required": False,
        },
    }
    bot._get_pending_multi_plan = lambda _session: pending
    bot._reply_logged = AsyncMock()
    bot._execute_approved_plan_action = AsyncMock()
    query = SimpleNamespace(
        data="lc:plan:approve:0123456789abcdef",
        answer=AsyncMock(),
        message=SimpleNamespace(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_message=query.message,
    )

    await bot.handle_run_action(update, SimpleNamespace())

    bot._execute_approved_plan_action.assert_not_awaited()
    assert "blocked" in bot._reply_logged.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_pending_plan_execution_refuses_hidden_commands():
    bot = LightClawBot.__new__(LightClawBot)
    bot._get_pending_multi_plan = lambda _session: {
        "review": {"approval_blocked": True}
    }
    bot._reply_logged = AsyncMock()

    await bot._execute_pending_multi_plan_impl(SimpleNamespace(), "456")

    assert "blocked" in bot._reply_logged.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_shared_execution_gate_requires_second_confirmation():
    bot = LightClawBot.__new__(LightClawBot)
    pending = {
        "approval_id": "0123456789abcdef",
        "review": {
            "second_confirmation_required": True,
            "second_confirmation_prompted": False,
            "second_confirmed": False,
        },
    }
    bot._get_pending_multi_plan = lambda _session: pending
    bot._reply_logged = AsyncMock()
    bot._execute_multi_agent_plan = AsyncMock()

    await bot._execute_pending_multi_plan_impl(SimpleNamespace(), "456")

    bot._execute_multi_agent_plan.assert_not_awaited()
    assert pending["review"]["second_confirmation_prompted"] is True
    keyboard = bot._reply_logged.await_args.kwargs["reply_markup"]
    buttons = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert "lc:plan:confirm-risk:0123456789abcdef" in buttons


@pytest.mark.asyncio
async def test_multi_agent_setup_failure_explains_approval_was_consumed():
    bot = LightClawBot.__new__(LightClawBot)
    bot._get_pending_multi_plan = lambda _session: {
        "goal": "review the patch",
        "workers": [("builder", "codex"), ("auditor", "claude")],
        "plan_payload": {"workers": []},
        "review": {"second_confirmation_required": False},
    }
    bot._clear_pending_multi_plan = Mock(return_value=None)
    bot._active_run_ids_by_session = {}
    bot._reply_logged = AsyncMock()
    bot._execute_multi_agent_plan = AsyncMock(side_effect=OSError("workspace unavailable"))

    await bot._execute_pending_multi_plan_impl(SimpleNamespace(), "456")

    bot._execute_multi_agent_plan.assert_awaited_once()
    bot._clear_pending_multi_plan.assert_called_once_with("456")
    response = bot._reply_logged.await_args.args[1]
    assert "No agent was started" in response
    assert "approve a new plan" in response


@pytest.mark.asyncio
async def test_text_confirmation_refuses_hidden_commands():
    bot = LightClawBot.__new__(LightClawBot)
    bot._session_id_from_update = lambda _update: "456"
    bot._log_user_message = Mock()
    bot._get_pending_multi_plan = lambda _session: {
        "review": {"approval_blocked": True}
    }
    bot._classify_pending_multi_reply = lambda _text: "confirm"
    bot._reply_logged = AsyncMock()
    bot._execute_pending_multi_plan = AsyncMock()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=456))

    await bot._process_user_message(update, SimpleNamespace(), "yes")

    assert "blocked" in bot._reply_logged.await_args.args[1].lower()
    bot._execute_pending_multi_plan.assert_not_awaited()


def test_inline_keyboards_cover_required_plan_and_result_actions():
    bot = LightClawBot.__new__(LightClawBot)
    approval_id = "0123456789abcdef"
    plan_data = [
        button.callback_data
        for row in bot._inline_plan_keyboard(approval_id).inline_keyboard
        for button in row
    ]
    run_id = "multi-run-42"
    run_token = bot._run_action_token(run_id)
    result_data = [
        button.callback_data
        for row in bot._inline_result_keyboard(
            run_id, ["a" + "b" * 31]
        ).inline_keyboard
        for button in row
    ]
    assert {
        f"lc:plan:approve:{approval_id}",
        f"lc:plan:edit:{approval_id}",
        f"lc:plan:deny:{approval_id}",
    } <= set(plan_data)
    assert {
        f"lc:run:diff:{run_token}",
        f"lc:run:accept:{run_token}",
    } <= set(result_data)
    retry_action = f"lc:run:retry:{run_token}:a{'b' * 31}"
    assert retry_action in result_data
    assert len(retry_action.encode("utf-8")) <= 64
    cancel_data = [
        button.callback_data
        for row in bot._inline_cancel_keyboard(run_id).inline_keyboard
        for button in row
    ]
    assert cancel_data == [f"lc:run:cancel:{run_token}"]


@pytest.mark.asyncio
async def test_32_character_retry_button_reaches_job_store():
    bot = LightClawBot.__new__(LightClawBot)
    run_id, label = "multi-run-42", "a" + "b" * 31
    bot.is_update_allowed = lambda _update: True
    bot._session_id_from_update = lambda _update: "456"
    bot._last_run_ids_by_session = {"456": run_id}
    bot._result_actions_in_flight = set()
    bot.jobs = SimpleNamespace(retry_lane=Mock(return_value={"run_id": "retry-1"}))
    bot._reply_logged = AsyncMock()
    query = SimpleNamespace(
        data=f"lc:run:retry:{bot._run_action_token(run_id)}:{label}",
        answer=AsyncMock(),
        message=SimpleNamespace(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_message=query.message,
    )

    await bot.handle_run_action(update, SimpleNamespace())

    bot.jobs.retry_lane.assert_called_once_with(run_id, label)


@pytest.mark.asyncio
async def test_voice_transcription_survives_typing_failure_and_waits_for_approval(monkeypatch):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(groq_api_key="fixture")
    bot.is_update_allowed = lambda _update: True
    bot._pending_voice_goal_by_session = {}
    bot._privileged_request_times = {}
    bot._reply_logged = AsyncMock()
    bot._process_user_message = AsyncMock()
    monkeypatch.setattr("core.bot.handlers.transcribe_voice", AsyncMock(return_value="Build the fixture"))

    voice_file = SimpleNamespace(
        file_size=None,
        download_as_bytearray=AsyncMock(return_value=bytearray(b"audio")),
    )
    voice = SimpleNamespace(file_size=None, get_file=AsyncMock(return_value=voice_file))
    message = SimpleNamespace(voice=voice, caption="", reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        message=message,
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(
            send_chat_action=AsyncMock(side_effect=NetworkError("fixture failure"))
        )
    )

    await bot.handle_voice(update, context)

    bot._process_user_message.assert_not_awaited()
    assert bot._pending_voice_goal_by_session["456"]["transcription"] == "Build the fixture"
    call = bot._reply_logged.await_args
    assert "not executed" in call.args[1]
    assert call.kwargs["reply_markup"] is not None
    voice_buttons = [
        button.callback_data
        for row in call.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    approval_id = bot._pending_voice_goal_by_session["456"]["approval_id"]
    assert f"lc:voice:approve:{approval_id}" in voice_buttons


@pytest.mark.asyncio
async def test_voice_without_groq_key_is_rejected_before_download():
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(groq_api_key="")
    bot.is_update_allowed = lambda _update: True
    bot._reply_logged = AsyncMock()
    get_file = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        message=SimpleNamespace(voice=SimpleNamespace(get_file=get_file)),
    )
    send_chat_action = AsyncMock()
    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=send_chat_action))

    await bot.handle_voice(update, context)

    get_file.assert_not_awaited()
    send_chat_action.assert_not_awaited()
    assert "unavailable" in bot._reply_logged.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_high_risk_callback_requires_ordered_second_confirmation():
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    pending = {
        "approval_id": "0123456789abcdef",
        "review": {
            "second_confirmation_required": True,
            "second_confirmation_prompted": False,
            "second_confirmed": False,
        }
    }
    bot._get_pending_multi_plan = lambda _session: pending
    bot._reply_logged = AsyncMock()
    bot._execute_approved_plan_action = AsyncMock()
    query = SimpleNamespace(
        data="lc:plan:confirm-risk:0123456789abcdef",
        answer=AsyncMock(),
        message=SimpleNamespace(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_message=query.message,
    )
    context = SimpleNamespace()

    await bot.handle_run_action(update, context)
    bot._execute_approved_plan_action.assert_not_awaited()

    query.data = "lc:plan:approve:0123456789abcdef"
    await bot.handle_run_action(update, context)
    assert pending["review"]["second_confirmation_prompted"] is True
    bot._execute_approved_plan_action.assert_not_awaited()

    query.data = "lc:plan:confirm-risk:0123456789abcdef"
    await bot.handle_run_action(update, context)
    bot._execute_approved_plan_action.assert_awaited_once()
    assert pending["review"]["second_confirmed"] is True


@pytest.mark.asyncio
async def test_text_confirmation_prompts_for_second_high_risk_confirmation():
    bot = LightClawBot.__new__(LightClawBot)
    pending = {
        "approval_id": "0123456789abcdef",
        "review": {
            "second_confirmation_required": True,
            "second_confirmation_prompted": False,
            "second_confirmed": False,
        },
    }
    bot._log_user_message = lambda *_args: None
    bot._get_pending_multi_plan = lambda _session: pending
    bot._classify_pending_multi_reply = lambda _text: "confirm"
    bot._reply_logged = AsyncMock()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=456, type="private"), effective_user=None
    )

    await bot._process_user_message(update, SimpleNamespace(), "yes")

    assert pending["review"]["second_confirmation_prompted"] is True
    keyboard = bot._reply_logged.await_args.kwargs["reply_markup"]
    buttons = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert "lc:plan:confirm-risk:0123456789abcdef" in buttons


@pytest.mark.asyncio
async def test_stale_or_unknown_plan_callback_cannot_execute():
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    bot._pending_multi_plan_by_session = {}
    bot._pending_multi_plan_ttl_sec = 900
    old_plan = bot._set_pending_multi_plan(
        "456", {"review": {"second_confirmation_required": False}}
    )
    current_plan = bot._set_pending_multi_plan(
        "456", {"review": {"second_confirmation_required": False}}
    )
    assert old_plan["approval_id"] != current_plan["approval_id"]
    bot._reply_logged = AsyncMock()
    bot._execute_approved_plan_action = AsyncMock()
    query = SimpleNamespace(
        data=f"lc:plan:approve:{old_plan['approval_id']}",
        answer=AsyncMock(),
        message=SimpleNamespace(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_message=query.message,
    )

    await bot.handle_run_action(update, SimpleNamespace())
    query.data = f"lc:plan:unexpected:{current_plan['approval_id']}"
    await bot.handle_run_action(update, SimpleNamespace())

    bot._execute_approved_plan_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_plan_approval_callbacks_start_one_run():
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    bot._session_id_from_update = lambda _update: "456"
    bot._pending_multi_plan_by_session = {}
    bot._pending_multi_plan_ttl_sec = 900
    bot._session_run_locks = {}
    bot._active_run_tasks_by_session = {}
    bot._active_run_ids_by_session = {}
    bot._reply_logged = AsyncMock()
    approval_id = bot._set_pending_multi_plan(
        "456",
        {
            "goal": "review the patch",
            "workers": [("builder", "codex"), ("auditor", "claude")],
            "plan_payload": {"workers": []},
            "review": {"second_confirmation_required": False},
        },
    )["approval_id"]
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute_plan(**_kwargs):
        started.set()
        await release.wait()

    bot._execute_multi_agent_plan = AsyncMock(side_effect=execute_plan)

    def make_update():
        query = SimpleNamespace(
            data=f"lc:plan:approve:{approval_id}",
            answer=AsyncMock(),
            message=SimpleNamespace(),
        )
        return SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=456, type="private"),
            effective_message=query.message,
        )

    first = asyncio.create_task(bot.handle_run_action(make_update(), SimpleNamespace()))
    await started.wait()
    await bot.handle_run_action(make_update(), SimpleNamespace())
    bot._execute_multi_agent_plan.assert_awaited_once()
    release.set()
    await first


@pytest.mark.asyncio
async def test_stale_voice_approval_cannot_process_replacement_transcription():
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    pending = {
        "456": {
            "approval_id": "0123456789abcdef",
            "text": "new transcription",
            "expires_at": 10**20,
        }
    }
    bot._pending_voice_goal_by_session = pending
    bot._reply_logged = AsyncMock()
    bot._process_user_message = AsyncMock()
    query = SimpleNamespace(
        data="lc:voice:approve:0000000000000001",
        answer=AsyncMock(),
        message=SimpleNamespace(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_message=query.message,
    )

    await bot.handle_run_action(update, SimpleNamespace())

    bot._process_user_message.assert_not_awaited()
    assert pending["456"]["text"] == "new transcription"


@pytest.mark.asyncio
async def test_expired_voice_approval_does_not_process_transcription():
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    bot._pending_voice_goal_by_session = {
        "456": {
            "approval_id": "0123456789abcdef",
            "text": "expired transcription",
            "expires_at": 10**20,
            "expires_monotonic": 0,
        }
    }
    bot._reply_logged = AsyncMock()
    bot._process_user_message = AsyncMock()
    query = SimpleNamespace(
        data="lc:voice:approve:0123456789abcdef",
        answer=AsyncMock(),
        message=SimpleNamespace(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_message=query.message,
    )

    await bot.handle_run_action(update, SimpleNamespace())

    bot._process_user_message.assert_not_awaited()
    assert "456" not in bot._pending_voice_goal_by_session
    assert "expired" in bot._reply_logged.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_expired_trusted_confirmation_does_not_start_host_run():
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    bot._privileged_rate_limited = lambda *_args, **_kwargs: False
    bot._session_id_from_update = lambda _update: "456"
    bot._log_user_message = Mock()
    bot._pending_trusted_agent_run_by_session = {
        "456": {
            "agent": "codex",
            "task": "delete an external path",
            "expires_at": 10**20,
            "expires_monotonic": 0,
        }
    }
    bot._reply_logged = AsyncMock()
    bot._execute_one_shot_delegation = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        message=SimpleNamespace(),
    )

    await bot.cmd_agent(update, SimpleNamespace(args=["trusted", "confirm"]))

    bot._execute_one_shot_delegation.assert_not_awaited()
    assert "456" not in bot._pending_trusted_agent_run_by_session
    assert "no pending" in bot._reply_logged.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_stale_result_button_cannot_accept_a_newer_run():
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    bot._last_run_ids_by_session = {"456": "new-run"}
    bot._reply_logged = AsyncMock()
    bot._accept_last_run_result = AsyncMock()
    old_token = bot._run_action_token("old-run")
    query = SimpleNamespace(
        data=f"lc:run:accept:{old_token}",
        answer=AsyncMock(),
        message=SimpleNamespace(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_message=query.message,
    )

    await bot.handle_run_action(update, SimpleNamespace())

    bot._accept_last_run_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_cancel_button_cannot_cancel_a_newer_active_run():
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    bot._active_run_ids_by_session = {"456": "new-run"}
    cancel_task = Mock()
    bot._active_run_tasks_by_session = {"456": cancel_task}
    bot._reply_logged = AsyncMock()
    old_token = bot._run_action_token("old-run")
    query = SimpleNamespace(
        data=f"lc:run:cancel:{old_token}",
        answer=AsyncMock(),
        message=SimpleNamespace(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_message=query.message,
    )

    await bot.handle_run_action(update, SimpleNamespace())

    cancel_task.cancel.assert_not_called()
    bot._reply_logged.assert_awaited_once()
    assert "no longer active" in bot._reply_logged.await_args.args[1]


@pytest.mark.asyncio
async def test_active_cancel_button_requests_the_matching_run():
    bot = LightClawBot.__new__(LightClawBot)
    run_id = "active-run"
    bot.is_update_allowed = lambda _update: True
    bot._active_run_ids_by_session = {"456": run_id}
    waiting = asyncio.Event()
    task = asyncio.create_task(waiting.wait())
    bot._active_run_tasks_by_session = {"456": task}
    canceled: list[str] = []
    bot.jobs = SimpleNamespace(request_cancel=lambda candidate: canceled.append(candidate))
    bot._reply_logged = AsyncMock()
    query = SimpleNamespace(
        data=f"lc:run:cancel:{bot._run_action_token(run_id)}",
        answer=AsyncMock(),
        message=SimpleNamespace(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_message=query.message,
    )

    await bot.handle_run_action(update, SimpleNamespace())

    assert canceled == [run_id]
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_duplicate_cancel_does_not_interrupt_run_cleanup():
    bot = LightClawBot.__new__(LightClawBot)
    run_id = "active-run"
    bot.is_update_allowed = lambda _update: True
    bot._active_run_ids_by_session = {"456": run_id}
    bot.jobs = SimpleNamespace(request_cancel=Mock())
    bot._reply_logged = AsyncMock()
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    finish_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def run_with_cleanup():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await finish_cleanup.wait()
            cleanup_finished.set()
            raise

    task = asyncio.create_task(run_with_cleanup())
    bot._active_run_tasks_by_session = {"456": task}

    def make_update():
        query = SimpleNamespace(
            data=f"lc:run:cancel:{bot._run_action_token(run_id)}",
            answer=AsyncMock(),
            message=SimpleNamespace(),
        )
        return SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=456, type="private"),
            effective_message=query.message,
        )

    await started.wait()
    await bot.handle_run_action(make_update(), SimpleNamespace())
    await cleanup_started.wait()
    await bot.handle_run_action(make_update(), SimpleNamespace())

    await asyncio.sleep(0)
    assert not task.done()
    finish_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup_finished.is_set()


@pytest.mark.asyncio
async def test_cancelled_multi_plan_stops_workers_and_releases_durable_job(
    tmp_path, monkeypatch
):
    bot = LightClawBot.__new__(LightClawBot)
    bot._pending_multi_plan_by_session = {}
    bot._pending_multi_plan_ttl_sec = 900
    bot._set_pending_multi_plan(
        "456",
        {
            "goal": "fixture multi run",
            "workers": [("builder", "codex"), ("reviewer", "claude")],
            "plan_payload": {"workers": []},
        },
    )
    bot.jobs = JobStore(tmp_path / "jobs.db")
    bot._active_run_ids_by_session = {}
    bot._active_run_tasks_by_session = {}
    bot._active_worker_tasks_by_run = {}
    bot._active_run_heartbeats_by_run = {}
    update_lane = bot.jobs.update_lane
    fail_once = True
    worker_started = asyncio.Event()
    worker_cleaned = asyncio.Event()
    never = asyncio.Event()
    run_id_holder: list[str] = []
    heartbeat_holder: list[asyncio.Task] = []

    def fail_cancel_once(*args, **kwargs):
        nonlocal fail_once
        if fail_once and args[2] == "canceled":
            fail_once = False
            raise sqlite3.OperationalError("database is locked")
        return update_lane(*args, **kwargs)

    monkeypatch.setattr(bot.jobs, "update_lane", fail_cancel_once)

    async def fake_execute_multi_agent_plan(**kwargs):
        run_id = kwargs["run_id"]
        run_id_holder.append(run_id)
        bot.jobs.create_job(
            workspace=tmp_path / "repo",
            session_id="456",
            goal="fixture multi run",
            approved_scope="fixture",
            risk_level="medium",
            capability_profile="workspace-write",
            plan=[
                {
                    "label": "builder",
                    "depends_on": [],
                    "owned_paths": [],
                    "idempotent": False,
                    "resumable": False,
                }
            ],
            status="queued",
            run_id=run_id,
        )
        bot.jobs.claim_next(workspace=tmp_path / "repo", worker_pid=999999)
        bot.jobs.update_lane(run_id, "builder", "running")
        bot._active_run_ids_by_session["456"] = run_id

        async def worker():
            worker_started.set()
            try:
                await never.wait()
            finally:
                worker_cleaned.set()

        async def heartbeat():
            await never.wait()

        worker_task = asyncio.create_task(worker())
        heartbeat_task = asyncio.create_task(heartbeat())
        bot._active_worker_tasks_by_run[run_id] = {worker_task}
        bot._active_run_heartbeats_by_run[run_id] = heartbeat_task
        heartbeat_holder.append(heartbeat_task)
        await never.wait()

    bot._execute_multi_agent_plan = fake_execute_multi_agent_plan
    execution = asyncio.create_task(
        bot._execute_pending_multi_plan(SimpleNamespace(), "456")
    )
    await worker_started.wait()
    execution.cancel()

    with pytest.raises(asyncio.CancelledError):
        await execution

    job = bot.jobs.get_job(run_id_holder[0])
    assert job["status"] == "canceled"
    assert job["lanes"][0]["status"] == "canceled"
    assert worker_cleaned.is_set()
    assert heartbeat_holder[0].cancelled()
    assert "456" not in bot._active_run_ids_by_session
    bot.jobs.close()


@pytest.mark.asyncio
async def test_cancelled_multi_workspace_creation_removes_late_owned_directory(
    tmp_path, monkeypatch
):
    root = tmp_path / "workspace"
    root.mkdir()
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(root))
    bot.jobs = JobStore(tmp_path / "jobs.db")
    bot._pending_multi_plan_by_session = {}
    bot._pending_multi_plan_ttl_sec = 900
    bot._set_pending_multi_plan(
        "456",
        {
            "goal": "cancel during workspace creation",
            "workers": [("builder", "codex"), ("reviewer", "claude")],
            "plan_payload": {"workers": []},
        },
    )
    bot._active_run_ids_by_session = {}
    bot._active_run_tasks_by_session = {}
    bot._active_worker_tasks_by_run = {}
    bot._active_run_heartbeats_by_run = {}
    bot._reply_logged = AsyncMock()
    started = threading.Event()
    release = threading.Event()
    from core.bot.delegation.workspace import register_task_workspace

    def delayed_register(workspace_root, candidate, goal):
        started.set()
        assert release.wait(timeout=5)
        register_task_workspace(workspace_root, candidate, goal)

    monkeypatch.setattr(
        "core.bot.delegation.workspace.register_task_workspace", delayed_register
    )
    execution = asyncio.create_task(
        bot._execute_pending_multi_plan(SimpleNamespace(), "456")
    )
    assert await asyncio.to_thread(started.wait, 5)
    execution.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await execution

    assert [path.name for path in root.iterdir()] == [".lightclaw-meta"]
    metadata = list((root / ".lightclaw-meta").glob("*.json"))
    assert len(metadata) == 1
    assert json.loads(metadata[0].read_text(encoding="utf-8"))["state"] == "undone"
    bot.jobs.close()


@pytest.mark.asyncio
async def test_failed_multi_workspace_preflight_removes_unannounced_workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(root))
    bot.jobs = JobStore(tmp_path / "jobs.db")
    bot._pending_multi_plan_by_session = {}
    bot._pending_multi_plan_ttl_sec = 900
    bot._set_pending_multi_plan(
        "456",
        {
            "goal": "fail before workspace announcement",
            "workers": [("builder", "codex"), ("reviewer", "claude")],
            "plan_payload": {"workers": []},
        },
    )
    bot._active_run_ids_by_session = {}
    bot._active_run_tasks_by_session = {}
    bot._active_worker_tasks_by_run = {}
    bot._active_run_heartbeats_by_run = {}
    bot._reply_logged = AsyncMock()
    bot._write_agents_plan_file = Mock(side_effect=OSError("workspace is read-only"))

    await bot._execute_pending_multi_plan(SimpleNamespace(), "456")

    assert [path.name for path in root.iterdir()] == [".lightclaw-meta"]
    metadata = list((root / ".lightclaw-meta").glob("*.json"))
    assert len(metadata) == 1
    assert json.loads(metadata[0].read_text(encoding="utf-8"))["state"] == "undone"
    assert "No agent was started" in bot._reply_logged.await_args.args[1]
    bot.jobs.close()


@pytest.mark.asyncio
async def test_unexpected_multi_plan_failure_cleans_workers_and_fails_job(tmp_path):
    bot = LightClawBot.__new__(LightClawBot)
    bot._pending_multi_plan_by_session = {}
    bot._pending_multi_plan_ttl_sec = 900
    bot._set_pending_multi_plan(
        "456",
        {
            "goal": "fixture multi run",
            "workers": [("builder", "codex"), ("reviewer", "claude")],
            "plan_payload": {"workers": []},
        },
    )
    bot.jobs = JobStore(tmp_path / "jobs.db")
    bot._active_run_ids_by_session = {}
    bot._active_run_tasks_by_session = {}
    bot._active_worker_tasks_by_run = {}
    bot._active_run_heartbeats_by_run = {}
    bot._reply_logged = AsyncMock()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    worker_started = asyncio.Event()
    worker_cleaned = asyncio.Event()
    never = asyncio.Event()
    run_id_holder: list[str] = []
    heartbeat_holder: list[asyncio.Task] = []

    async def fake_execute_multi_agent_plan(**kwargs):
        run_id = kwargs["run_id"]
        run_id_holder.append(run_id)
        bot.jobs.create_job(
            workspace=workspace,
            session_id="456",
            goal="fixture multi run",
            approved_scope="fixture",
            risk_level="medium",
            capability_profile="workspace-write",
            plan=[
                {
                    "label": "builder",
                    "depends_on": [],
                    "owned_paths": ["src.py"],
                    "idempotent": True,
                    "resumable": True,
                },
                {
                    "label": "reviewer",
                    "depends_on": ["builder"],
                    "owned_paths": ["tests.py"],
                    "idempotent": True,
                    "resumable": True,
                },
            ],
            status="queued",
            run_id=run_id,
        )
        bot.jobs.claim_next(workspace=workspace, worker_pid=999999)
        bot.jobs.update_lane(run_id, "builder", "running")
        bot._active_run_ids_by_session["456"] = run_id

        async def worker():
            worker_started.set()
            try:
                await never.wait()
            finally:
                worker_cleaned.set()

        async def heartbeat():
            await never.wait()

        worker_task = asyncio.create_task(worker())
        heartbeat_task = asyncio.create_task(heartbeat())
        bot._active_worker_tasks_by_run[run_id] = {worker_task: "builder"}
        bot._active_run_heartbeats_by_run[run_id] = heartbeat_task
        heartbeat_holder.append(heartbeat_task)
        await worker_started.wait()
        raise RuntimeError("fixture orchestration failure")

    bot._execute_multi_agent_plan = fake_execute_multi_agent_plan
    with pytest.raises(RuntimeError, match="fixture orchestration failure"):
        await bot._execute_pending_multi_plan(SimpleNamespace(), "456")

    run_id = run_id_holder[0]
    job = bot.jobs.get_job(run_id)
    assert job["status"] == "failed"
    assert [lane["status"] for lane in job["lanes"]] == ["failed", "failed"]
    assert worker_cleaned.is_set()
    assert heartbeat_holder[0].cancelled()
    assert bot._active_worker_tasks_by_run == {}
    assert bot._active_run_heartbeats_by_run == {}
    assert bot._active_run_ids_by_session == {}
    assert bot._active_run_tasks_by_session == {}
    bot._reply_logged.assert_awaited_once()
    bot.jobs.close()


@pytest.mark.asyncio
async def test_duplicate_result_taps_only_run_one_acceptance():
    bot = LightClawBot.__new__(LightClawBot)
    run_id = "run-17"
    bot.is_update_allowed = lambda _update: True
    bot._last_run_ids_by_session = {"456": run_id}
    bot._result_actions_in_flight = set()
    bot._reply_logged = AsyncMock()
    started = asyncio.Event()
    release = asyncio.Event()

    async def accept(*_args):
        started.set()
        await release.wait()

    bot._accept_last_run_result = AsyncMock(side_effect=accept)

    def make_update():
        query = SimpleNamespace(
            data=f"lc:run:accept:{bot._run_action_token(run_id)}",
            answer=AsyncMock(),
            message=SimpleNamespace(),
        )
        return SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=456, type="private"),
            effective_message=query.message,
        )

    first = asyncio.create_task(bot.handle_run_action(make_update(), SimpleNamespace()))
    await started.wait()
    await bot.handle_run_action(make_update(), SimpleNamespace())
    assert bot._accept_last_run_result.await_count == 1
    release.set()
    await first
    assert bot._result_actions_in_flight == set()


@pytest.mark.asyncio
async def test_view_diff_sends_compact_summary_before_patch(tmp_path):
    patch_path = tmp_path / "changes.patch"
    patch_content = (
        "diff --git a/file b/file\n"
        "--- a/file\n+++ b/file\n@@ -1 +1,2 @@\n-old\n+new\n+added\n"
    )
    patch_path.write_text(patch_content, encoding="utf-8")
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "run_id": "run-18",
                "diff_summary": "large file-by-file stat\n10 files changed, 7 insertions(+), 2 deletions(-)",
                "file_changes": [
                    {"change": "modified", "path": "src/main.py"},
                    {"change": "added", "path": "tests/test_main.py"},
                    {"change": "deleted", "path": "old.txt"},
                    *[
                        {"change": "modified", "path": f"extra/file-{index}.py"}
                        for index in range(7)
                    ],
                ],
                "artifacts": [str(patch_path)],
            }
        ),
        encoding="utf-8",
    )
    events = []
    private_file = tmp_path / "private.txt"
    private_file.write_text("must not be sent", encoding="utf-8")

    async def send_summary(_update, text):
        patch_path.unlink()
        patch_path.symlink_to(private_file)
        events.append(("summary", text))

    async def send_patch(**kwargs):
        document = kwargs["document"]
        assert isinstance(document, InputFile)
        assert not document.input_file_content.closed
        assert not isinstance(document.input_file_content, bytes)
        document.input_file_content.seek(0)
        assert document.input_file_content.read() == patch_content.encode("utf-8")
        events.append(("patch", ""))

    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(tmp_path))
    bot._last_run_receipts_by_session = {"456": str(receipt_path)}
    bot._reply_logged = AsyncMock(side_effect=send_summary)
    message = SimpleNamespace(reply_document=AsyncMock(side_effect=send_patch))
    update = SimpleNamespace(message=message)

    await bot._send_last_run_diff(update, "456", "run-18")

    assert [kind for kind, _ in events] == ["summary", "patch"]
    assert "10 files changed, 7 insertions(+), 2 deletions(-)" in events[0][1]
    assert "src/main.py" in events[0][1]
    assert "and 2 more files" in events[0][1]
    assert "extra/file-4.py" in events[0][1]
    assert "extra/file-6.py" not in events[0][1]
    assert "Patch preview (first text hunk)" in events[0][1]
    assert "-old" in events[0][1] and "+new" in events[0][1]
    assert "nothing has been accepted or pushed" in events[0][1]
    message.reply_document.assert_awaited_once()
    assert bot._mobile_diff_preview(
        "diff --git a/logo.png b/logo.png\nGIT binary patch\ndata"
    ) == "No text hunk; full patch attached."


@pytest.mark.asyncio
async def test_view_diff_skips_patch_larger_than_telegram_upload_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("core.bot.approvals.TELEGRAM_BOT_API_MAX_FILE_BYTES", 8)
    patch_path = tmp_path / "changes.patch"
    patch_path.write_bytes(b"x" * 9)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "run_id": "run-large-patch",
                "diff_summary": "1 file changed",
                "file_changes": [],
                "artifacts": [str(patch_path)],
            }
        ),
        encoding="utf-8",
    )
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(tmp_path))
    bot._last_run_receipts_by_session = {"456": str(receipt_path)}
    bot._reply_logged = AsyncMock()
    message = SimpleNamespace(reply_document=AsyncMock())

    await bot._send_last_run_diff(
        SimpleNamespace(message=message), "456", "run-large-patch"
    )

    message.reply_document.assert_not_awaited()
    assert "too large to attach" in bot._reply_logged.await_args.args[1]
    assert patch_path.as_posix() in bot._reply_logged.await_args.args[1]


@pytest.mark.asyncio
async def test_view_diff_handles_oversized_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr("core.receipts.MAX_RECEIPT_BYTES", 8)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_bytes(b" " * 9)
    bot = LightClawBot.__new__(LightClawBot)
    bot._last_run_receipts_by_session = {"456": str(receipt_path)}
    bot._reply_logged = AsyncMock()

    await bot._send_last_run_diff(SimpleNamespace(message=None), "456", "run-18")

    bot._reply_logged.assert_awaited_once_with(
        SimpleNamespace(message=None), "The local run receipt is unavailable."
    )


@pytest.mark.asyncio
async def test_long_result_is_private_file_artifact_not_chat_wall(tmp_path):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(tmp_path / "workspace"))

    async def send_artifact(**kwargs):
        document = kwargs["document"]
        assert isinstance(document, InputFile)
        assert not document.input_file_content.closed
        assert not isinstance(document.input_file_content, bytes)

    message = SimpleNamespace(
        reply_document=AsyncMock(side_effect=send_artifact), reply_text=AsyncMock()
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=456, type="private"),
        message=message,
    )

    await bot._send_response(None, update, "evidence\n" * 1000)

    message.reply_document.assert_awaited_once()
    message.reply_text.assert_not_awaited()
    files = list((tmp_path / "workspace" / ".lightclaw-meta" / "messages").glob("*.md"))
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert files[0].read_text(encoding="utf-8").startswith("evidence")


@pytest.mark.asyncio
async def test_long_result_refuses_symlinked_private_metadata(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / ".lightclaw-meta").symlink_to(outside, target_is_directory=True)
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(workspace))
    placeholder = SimpleNamespace(edit_text=AsyncMock())
    update = SimpleNamespace(message=None)

    with pytest.raises(WorkspaceSafetyError, match="must not be a symlink"):
        bot._write_long_response_artifact("private result")

    await bot._send_response(placeholder, update, "private result\n" * 1000)

    placeholder.edit_text.assert_awaited_once()
    assert "Could not save this long result safely" in placeholder.edit_text.await_args.args[0]
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_long_result_does_not_spam_chat_when_attachment_fails(tmp_path):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(tmp_path / "workspace"))
    message = SimpleNamespace(
        reply_document=AsyncMock(side_effect=RuntimeError("upload failed")),
        reply_text=AsyncMock(),
    )
    placeholder = SimpleNamespace(edit_text=AsyncMock())
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=456, type="private"),
        message=message,
    )

    await bot._send_response(placeholder, update, "evidence\n" * 1000)

    message.reply_document.assert_awaited_once()
    message.reply_text.assert_not_awaited()
    assert placeholder.edit_text.await_count == 2
    assert "saved locally" in placeholder.edit_text.await_args.args[0]
