from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import InputFile
from telegram.error import NetworkError

import core.bot.approvals as approvals
from config import Config
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


@pytest.mark.parametrize(("field", "caption"), [("expected_inputs", "inputs"), ("expected_outputs", "outputs")])
def test_plan_expected_inputs_and_outputs_are_visible_and_trigger_second_confirmation(field, caption):
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
                        field: ["Publish the production release"],
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
    assert f"{caption}: Publish the production release" in preview


def test_plan_review_makes_directional_controls_visible_in_paths_and_commands():
    bot = LightClawBot.__new__(LightClawBot)
    rendered = bot._render_plan_review(
        {
            "review": {
                "changed_paths": ["safe.py\u202eevil.txt\ud800"],
                "proposed_commands": ["cat safe.py\u202eevil.txt"],
            }
        }
    )

    assert "\u202e" not in rendered and "\ud800" not in rendered
    assert rendered.count("�") == 3


@pytest.mark.parametrize("terminal", [False, True])
def test_detailed_plan_preview_makes_untrusted_controls_visible(monkeypatch, terminal):
    monkeypatch.setenv("LIGHTCLAW_CHAT_MODE", "1" if terminal else "0")
    bot = LightClawBot.__new__(LightClawBot)
    controls = "\u202e\x00\ud800\x1b[31m"
    label = "builder" + controls
    rendered = bot._render_multi_plan_preview(
        goal="goal <unsafe>" + controls,
        workers=[(label, "codex" + controls)],
        plan_payload={"workers": [{
            "label": label,
            "role": "role" + controls,
            "depends_on": ["dependency" + controls],
            "responsibilities": ["responsibility" + controls],
            "expected_inputs": ["input" + controls],
            "expected_outputs": ["output" + controls],
            "owned_paths": ["path" + controls],
        }]},
        warnings=["warning" + controls],
    )

    assert "\u202e" not in rendered and "\x00" not in rendered and "\ud800" not in rendered
    assert "\x1b[31m" not in rendered
    for field in ("builder", "codex", "role", "dependency", "responsibility", "input", "output", "path", "warning"):
        assert field + "����[31m" in rendered
    assert "goal &lt;unsafe&gt;����[31m" in rendered
    assert "<b>Worker Contracts:</b>\n" in rendered
    assert ("\x1b[" in rendered) == terminal
    rendered.encode("utf-16-le")


def test_mobile_diff_preview_makes_directional_controls_visible():
    rendered = LightClawBot._mobile_diff_preview(
        "@@ -1 +1 @@\n-old\n+safe.py\u202eevil.txt"
    )

    assert "\u202e" not in rendered
    assert "�" in rendered


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
        "git checkout main -- config.env",
        "python -c \"import subprocess; subprocess.run(['git', 'restore', '.'])\"",
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
    expected_inputs = ["API contract & constraints", "Reviewer's handoff <notes>"]
    expected_outputs = ["Updated API", "Regression test report"]
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
                    "expected_inputs": expected_inputs,
                    "expected_outputs": expected_outputs,
                    "owned_paths": owned_paths,
                }
            ]
        },
    )

    assert all(item in rendered for item in responsibilities + owned_paths)
    assert "inputs: API contract &amp; constraints · Reviewer&#x27;s handoff &lt;notes&gt;" in rendered
    assert all(item in rendered for item in expected_outputs)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["goal", "expected_inputs"])
async def test_long_plan_preview_is_chunked_with_approval_on_final_chunk(field):
    bot = LightClawBot.__new__(LightClawBot)
    bot._reply_logged = AsyncMock()
    bot._session_id_from_update = lambda _update: "456"
    update = SimpleNamespace()
    approval_id = "0123456789abcdef"
    bot._pending_multi_plan_by_session = {
        "456": {"approval_id": approval_id, "review_delivered": False}
    }
    review_text = "x" * 3900 + "end-of-scope"
    preview = bot._render_multi_plan_preview(
        review_text if field == "goal" else "Review worker inputs",
        [("builder", "codex")],
        {"workers": [{"label": "builder", "expected_inputs": [review_text] if field == "expected_inputs" else []}]},
    )
    preview += "\n\n" + bot._render_plan_review({})

    await bot._reply_multi_plan_preview(update, preview, approval_id, False)

    calls = bot._reply_logged.await_args_list
    assert len(calls) > 1
    assert all(len(call.args[1]) < 4096 for call in calls)
    assert all(call.kwargs["reply_markup"] is None for call in calls[:-1])
    assert calls[-1].kwargs["reply_markup"] is not None
    assert review_text in "".join(bot._strip_html_for_log(call.args[1]) for call in calls)
    assert bot._pending_multi_plan_by_session["456"]["review_delivered"]


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
        "review_delivered": True,
        "approval_id": "0123456789abcdef",
        "user_id": 123,
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
        "user_id": 123,
        "review_delivered": True,
        "review": {"approval_blocked": True}
    }
    bot._reply_logged = AsyncMock()

    await bot._execute_pending_multi_plan_impl(
        SimpleNamespace(effective_user=SimpleNamespace(id=123)), "456"
    )

    assert "blocked" in bot._reply_logged.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_shared_execution_gate_requires_second_confirmation():
    bot = LightClawBot.__new__(LightClawBot)
    pending = {
        "review_delivered": True,
        "approval_id": "0123456789abcdef",
        "user_id": 123,
        "review": {
            "second_confirmation_required": True,
            "second_confirmation_prompted": False,
            "second_confirmed": False,
        },
    }
    bot._get_pending_multi_plan = lambda _session: pending
    bot._reply_logged = AsyncMock()
    bot._execute_multi_agent_plan = AsyncMock()

    await bot._execute_pending_multi_plan_impl(
        SimpleNamespace(effective_user=SimpleNamespace(id=123)), "456"
    )

    bot._execute_multi_agent_plan.assert_not_awaited()
    assert pending["review"]["second_confirmation_prompted"] is True
    keyboard = bot._reply_logged.await_args.kwargs["reply_markup"]
    buttons = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert "lc:plan:confirm-risk:0123456789abcdef" in buttons


@pytest.mark.asyncio
async def test_multi_agent_setup_failure_explains_approval_was_consumed():
    bot = LightClawBot.__new__(LightClawBot)
    bot._get_pending_multi_plan = lambda _session: {
        "user_id": 123,
        "review_delivered": True,
        "goal": "review the patch",
        "workers": [("builder", "codex"), ("auditor", "claude")],
        "plan_payload": {"workers": []},
        "review": {"second_confirmation_required": False},
    }
    bot._clear_pending_multi_plan = Mock(return_value=None)
    bot._active_run_ids_by_session = {}
    bot._reply_logged = AsyncMock()
    bot._execute_multi_agent_plan = AsyncMock(side_effect=OSError("workspace unavailable"))

    await bot._execute_pending_multi_plan_impl(
        SimpleNamespace(effective_user=SimpleNamespace(id=123)), "456"
    )

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
        "user_id": 123,
        "review_delivered": True,
        "review_message_id": 42,
        "review": {"approval_blocked": True}
    }
    bot._classify_pending_multi_reply = lambda _text: "confirm"
    bot._reply_logged = AsyncMock()
    bot._execute_pending_multi_plan = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
        message=SimpleNamespace(reply_to_message=SimpleNamespace(message_id=42)),
    )

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
@pytest.mark.parametrize("action", ["cancel", "accept", "reject", "retry", "diff"])
async def test_public_group_run_callbacks_enforce_requester_boundary(action):
    bot = LightClawBot.__new__(LightClawBot)
    run_id = "public-run"
    bot.is_update_allowed = lambda _update: True
    bot._session_id_from_update = lambda _update: "456"
    bot._active_run_ids_by_session = {"456": run_id}
    bot._active_run_requesters_by_session = {"456": 123}
    bot._active_run_tasks_by_session = {}
    bot._last_run_ids_by_session = {"456": run_id}
    bot._last_run_requesters_by_session = {"456": 123}
    bot._result_actions_in_flight = set()
    bot.jobs = SimpleNamespace(request_cancel=Mock(), retry_lane=Mock())
    bot._accept_last_run_result = AsyncMock()
    bot._reject_last_run_result = AsyncMock()
    bot._send_last_run_diff = AsyncMock()
    bot._reply_logged = AsyncMock()
    suffix = ":builder" if action == "retry" else ""
    query = SimpleNamespace(
        data=f"lc:run:{action}:{bot._run_action_token(run_id)}{suffix}",
        answer=AsyncMock(),
        message=SimpleNamespace(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=999),
        effective_chat=SimpleNamespace(id=-456, type="supergroup"),
        effective_message=query.message,
    )

    await bot.handle_run_action(update, SimpleNamespace())

    if action == "diff":
        bot._send_last_run_diff.assert_awaited_once()
        bot._reply_logged.assert_not_awaited()
    else:
        bot._reply_logged.assert_awaited_once()
        assert "Only the requester" in bot._reply_logged.await_args.args[1]
    bot.jobs.request_cancel.assert_not_called()
    bot.jobs.retry_lane.assert_not_called()
    bot._accept_last_run_result.assert_not_awaited()
    bot._reject_last_run_result.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("user_id", "preserve_pending"), [(999, True), (123, False)])
async def test_public_group_clear_preserves_other_members_pending_actions(
    user_id, preserve_pending
):
    bot = LightClawBot.__new__(LightClawBot)
    session_id = "-456"
    pending = {session_id: {"user_id": 123}}
    bot.is_update_allowed = lambda _update: True
    bot._privileged_rate_limited = lambda *_args, **_kwargs: False
    bot._session_scope_from_update = AsyncMock(return_value=session_id)
    bot._log_user_message = Mock()
    bot._invalidate_active_message_requests = Mock()
    bot._invalidate_session_summary = Mock()
    bot._session_summaries = {}
    bot._pending_wipe_confirm = {session_id: dict(pending[session_id])}
    bot._pending_multi_plan_by_session = {session_id: dict(pending[session_id])}
    bot._pending_trusted_agent_run_by_session = {session_id: dict(pending[session_id])}
    bot._pending_voice_goal_by_session = {session_id: dict(pending[session_id])}
    bot._voice_request_ids_by_session = {session_id: "voice-request"}
    bot.memory = SimpleNamespace(
        clear_session=Mock(),
        scope_for=Mock(return_value=("group", "/workspace")),
    )
    bot._reply_logged = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=-456, type="supergroup"),
        message=SimpleNamespace(),
    )

    await bot.cmd_clear(update, SimpleNamespace())

    bot.memory.clear_session.assert_called_once_with(session_id)
    bot._reply_logged.assert_awaited_once()
    assert "Other group members' pending actions were not changed." in (
        bot._reply_logged.await_args.args[1]
    )
    for pending_map in (
        bot._pending_wipe_confirm,
        bot._pending_multi_plan_by_session,
        bot._pending_trusted_agent_run_by_session,
        bot._pending_voice_goal_by_session,
    ):
        assert bool(pending_map) is preserve_pending
    assert bool(bot._voice_request_ids_by_session) is preserve_pending


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
    message = SimpleNamespace(voice=voice, caption="Review this change", reply_text=AsyncMock())
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
    assert bot._pending_voice_goal_by_session["456"]["text"] == (
        "Review this change\n[voice transcription: Build the fixture]"
    )
    assert bot._pending_voice_goal_by_session["456"]["user_id"] == 123
    call = bot._reply_logged.await_args
    assert "not executed" in call.args[1]
    assert "Review this change" in call.args[1]
    assert "Build the fixture" in call.args[1]
    assert "complete request" in call.args[1]
    assert call.kwargs["reply_markup"] is not None
    voice_buttons = [
        button.callback_data
        for row in call.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    approval_id = bot._pending_voice_goal_by_session["456"]["approval_id"]
    assert f"lc:voice:approve:{approval_id}" in voice_buttons


@pytest.mark.asyncio
async def test_group_voice_approval_is_bound_to_the_speaker():
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    bot._session_scope_from_update = AsyncMock(return_value="-7")
    bot._pending_voice_goal_by_session = {
        "-7": {
            "text": "[voice transcription: inspect the patch]",
            "approval_id": "0123456789abcdef",
            "user_id": 42,
            "expires_at": 10**20,
            "expires_monotonic": 10**20,
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
        effective_user=SimpleNamespace(id=99),
        effective_chat=SimpleNamespace(id=-7, type="group"),
        effective_message=query.message,
    )

    await bot.handle_run_action(update, SimpleNamespace())

    assert "-7" in bot._pending_voice_goal_by_session
    bot._process_user_message.assert_not_awaited()
    assert "only the user who sent" in bot._reply_logged.await_args.args[1].lower()

    update.effective_user.id = 42
    await bot.handle_run_action(update, SimpleNamespace())

    assert "-7" not in bot._pending_voice_goal_by_session
    bot._process_user_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_oversized_voice_transcription_is_not_left_pending(monkeypatch):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(groq_api_key="fixture")
    bot.is_update_allowed = lambda _update: True
    bot._pending_voice_goal_by_session = {}
    bot._privileged_request_times = {}
    bot._reply_logged = AsyncMock()
    monkeypatch.setattr(
        "core.bot.handlers.transcribe_voice", AsyncMock(return_value="🙂" * 2100)
    )
    voice_file = SimpleNamespace(
        file_size=None,
        download_as_bytearray=AsyncMock(return_value=bytearray(b"audio")),
    )
    voice = SimpleNamespace(file_size=None, get_file=AsyncMock(return_value=voice_file))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        message=SimpleNamespace(voice=voice, caption=""),
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))

    await bot.handle_voice(update, context)

    assert bot._pending_voice_goal_by_session == {}
    assert "too long to review" in bot._reply_logged.await_args.args[1]
    assert bot._reply_logged.await_args.kwargs.get("reply_markup") is None


@pytest.mark.asyncio
async def test_ambiguous_voice_delivery_keeps_callback_state(monkeypatch):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(groq_api_key="fixture")
    bot.is_update_allowed = lambda _update: True
    bot._pending_voice_goal_by_session = {}
    bot._privileged_request_times = {}
    bot._reply_logged = AsyncMock(side_effect=RuntimeError("response lost after delivery"))
    monkeypatch.setattr("core.bot.handlers.transcribe_voice", AsyncMock(return_value="hello"))
    voice_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"audio")))
    voice = SimpleNamespace(file_size=None, get_file=AsyncMock(return_value=voice_file))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        message=SimpleNamespace(voice=voice, caption=""),
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))

    with pytest.raises(RuntimeError, match="response lost"):
        await bot.handle_voice(update, context)

    assert bot._pending_voice_goal_by_session["456"]["text"] == "[voice transcription: hello]"


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
async def test_transcription_failure_suggests_retry_not_missing_key(monkeypatch):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(groq_api_key="fixture")
    bot.is_update_allowed = lambda _update: True
    bot._pending_voice_goal_by_session = {}
    bot._privileged_request_times = {}
    bot._reply_logged = AsyncMock()
    monkeypatch.setattr("core.bot.handlers.transcribe_voice", AsyncMock(return_value=None))
    voice_file = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"audio"))
    )
    voice = SimpleNamespace(file_size=None, get_file=AsyncMock(return_value=voice_file))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        message=SimpleNamespace(voice=voice, caption=""),
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))

    await bot.handle_voice(update, context)

    message = bot._reply_logged.await_args.args[1].lower()
    assert "failed" in message
    assert "retry" in message
    assert "groq_api_key" not in message


@pytest.mark.asyncio
async def test_high_risk_callback_requires_ordered_second_confirmation():
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    pending = {
        "review_delivered": True,
        "approval_id": "0123456789abcdef",
        "user_id": 123,
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
@pytest.mark.parametrize("failure", [None, NetworkError, asyncio.CancelledError])
async def test_second_confirmation_requires_successful_prompt_delivery(failure):
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    pending = {
        "review_delivered": True,
        "approval_id": "0123456789abcdef",
        "user_id": 123,
        "review": {
            "second_confirmation_required": True,
            "second_confirmation_prompted": False,
            "second_confirmed": False,
        },
    }
    bot._get_pending_multi_plan = lambda _session: pending
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(_update, text, **_kwargs):
        if "Second confirmation required" in text:
            entered.set()
            await release.wait()
            if failure:
                raise failure("delivery interrupted")

    bot._reply_logged = AsyncMock(side_effect=send)
    bot._execute_approved_plan_action = AsyncMock()
    query = SimpleNamespace(
        data="lc:plan:approve:0123456789abcdef", answer=AsyncMock(),
        message=SimpleNamespace(),
    )
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_message=query.message,
    )
    context = SimpleNamespace()
    prompt = asyncio.create_task(bot.handle_run_action(update, context))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        query.data = "lc:plan:confirm-risk:0123456789abcdef"
        await bot.handle_run_action(update, context)
        bot._execute_approved_plan_action.assert_not_awaited()
        assert pending["review"]["second_confirmed"] is False
        release.set()
        if failure:
            with pytest.raises(failure):
                await asyncio.wait_for(prompt, timeout=1)
        else:
            await asyncio.wait_for(prompt, timeout=1)
        await bot.handle_run_action(update, context)
        if failure:
            bot._execute_approved_plan_action.assert_not_awaited()
            assert pending["review"]["second_confirmation_prompted"] is False
        else:
            bot._execute_approved_plan_action.assert_awaited_once()
    finally:
        release.set()
        if not prompt.done():
            prompt.cancel()
        await asyncio.gather(prompt, return_exceptions=True)


@pytest.mark.asyncio
async def test_text_confirmation_prompts_for_second_high_risk_confirmation():
    bot = LightClawBot.__new__(LightClawBot)
    pending = {
        "review_delivered": True,
        "review_message_id": 42,
        "approval_id": "0123456789abcdef",
        "user_id": 123,
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
        effective_chat=SimpleNamespace(id=456, type="private"),
        effective_user=SimpleNamespace(id=123),
        message=SimpleNamespace(reply_to_message=SimpleNamespace(message_id=42)),
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
    bot._pending_multi_plan_by_session.clear()  # Simulate process restart.
    query.data = f"lc:plan:approve:{current_plan['approval_id']}"
    await bot.handle_run_action(update, SimpleNamespace())

    bot._execute_approved_plan_action.assert_not_awaited()
    assert "No pending plan" in bot._reply_logged.await_args.args[1]


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
            "user_id": 123,
        },
    )["approval_id"]
    bot._pending_multi_plan_by_session["456"]["review_delivered"] = True
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
            "user_id": 123,
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
async def test_trusted_confirmation_requires_requesting_group_user_and_runs_once():
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = Config(telegram_public_bot_ack=True)
    bot._privileged_rate_limited = lambda *_args, **_kwargs: False
    bot._log_user_message = Mock()
    bot._pending_trusted_agent_run_by_session = {}
    bot._reply_logged = AsyncMock()
    bot._execute_one_shot_delegation = AsyncMock()

    def update(user_id):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(id=-7, type="group"),
            message=SimpleNamespace(),
        )

    requester = update(42)
    await bot.cmd_agent(
        requester, SimpleNamespace(args=["trusted", "codex", "inspect", "external", "files"])
    )
    pending = dict(bot._pending_trusted_agent_run_by_session["-7"])
    await bot.cmd_agent(
        update(99),
        SimpleNamespace(args=["trusted", "codex", "replace", "the", "review"]),
    )
    assert bot._pending_trusted_agent_run_by_session["-7"] == pending
    assert "only the requester" in bot._reply_logged.await_args.args[1].lower()

    confirm = SimpleNamespace(args=["trusted", "confirm", pending["approval_id"]])

    await bot.cmd_agent(update(99), confirm)

    bot._execute_one_shot_delegation.assert_not_awaited()
    assert bot._pending_trusted_agent_run_by_session["-7"] == pending
    assert "requested" in bot._reply_logged.await_args.args[1]

    await bot.cmd_agent(requester, confirm)
    bot._execute_one_shot_delegation.assert_awaited_once_with(
        requester,
        session_id="-7",
        agent="codex",
        task="inspect external files",
        capability_profile="trusted-command",
    )
    assert "-7" not in bot._pending_trusted_agent_run_by_session

    await bot.cmd_agent(requester, confirm)
    assert bot._execute_one_shot_delegation.await_count == 1


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
async def test_cancel_button_stops_local_run_while_sqlite_write_waits():
    bot = LightClawBot.__new__(LightClawBot)
    run_id = "active-run"
    bot.is_update_allowed = lambda _update: True
    bot._active_run_ids_by_session = {"456": run_id}
    bot._reply_logged = AsyncMock()
    run_started = asyncio.Event()
    local_cancelled = asyncio.Event()
    request_started = threading.Event()
    release_request = threading.Event()

    async def active_run():
        run_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            local_cancelled.set()
            raise

    def slow_request(candidate):
        assert candidate == run_id
        request_started.set()
        release_request.wait(timeout=5)

    task = asyncio.create_task(active_run())
    bot._active_run_tasks_by_session = {"456": task}
    bot.jobs = SimpleNamespace(request_cancel=slow_request)
    await run_started.wait()

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
    callback = asyncio.create_task(bot.handle_run_action(update, SimpleNamespace()))
    assert await asyncio.to_thread(request_started.wait, 2)

    stopped_before_store = False
    try:
        await asyncio.wait_for(local_cancelled.wait(), timeout=0.2)
        stopped_before_store = True
    except asyncio.TimeoutError:
        pass
    finally:
        release_request.set()

    await callback
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped_before_store


@pytest.mark.asyncio
async def test_cancel_button_reports_durable_store_error_after_local_stop():
    bot = LightClawBot.__new__(LightClawBot)
    run_id = "active-run"
    bot.is_update_allowed = lambda _update: True
    bot._active_run_ids_by_session = {"456": run_id}
    bot._reply_logged = AsyncMock()
    task = asyncio.create_task(asyncio.Event().wait())
    bot._active_run_tasks_by_session = {"456": task}
    bot.jobs = SimpleNamespace(
        request_cancel=Mock(side_effect=OSError("job storage unavailable"))
    )
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

    with pytest.raises(asyncio.CancelledError):
        await task
    assert "could not be saved" in bot._reply_logged.await_args.args[1]


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
async def test_multi_plan_cancel_button_stops_workers_and_releases_durable_job(
    tmp_path, monkeypatch
):
    bot = LightClawBot.__new__(LightClawBot)
    bot.is_update_allowed = lambda _update: True
    bot._reply_logged = AsyncMock()
    bot._pending_multi_plan_by_session = {}
    bot._pending_multi_plan_ttl_sec = 900
    bot._set_pending_multi_plan(
        "456",
        {
            "goal": "fixture multi run",
            "workers": [("builder", "codex"), ("reviewer", "claude")],
            "plan_payload": {"workers": []},
            "user_id": 123,
        },
    )
    bot._pending_multi_plan_by_session["456"]["review_delivered"] = True
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
        bot._execute_pending_multi_plan(
            SimpleNamespace(effective_user=SimpleNamespace(id=123)), "456"
        )
    )
    await worker_started.wait()
    run_id = run_id_holder[0]
    assert bot._active_run_tasks_by_session["456"] is execution
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

    with pytest.raises(asyncio.CancelledError):
        await execution

    job = bot.jobs.get_job(run_id)
    assert job["status"] == "canceled"
    assert job["lanes"][0]["status"] == "canceled"
    assert worker_cleaned.is_set()
    assert heartbeat_holder[0].cancelled()
    assert "456" not in bot._active_run_ids_by_session
    assert "456" not in bot._active_run_tasks_by_session
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
            "user_id": 123,
        },
    )
    bot._pending_multi_plan_by_session["456"]["review_delivered"] = True
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
        bot._execute_pending_multi_plan(
            SimpleNamespace(effective_user=SimpleNamespace(id=123)), "456"
        )
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
async def test_failed_multi_workspace_preflight_removes_unclaimed_workspace(tmp_path):
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
            "user_id": 123,
        },
    )
    bot._pending_multi_plan_by_session["456"]["review_delivered"] = True
    bot._active_run_ids_by_session = {}
    bot._active_run_tasks_by_session = {}
    bot._active_worker_tasks_by_run = {}
    bot._active_run_heartbeats_by_run = {}
    bot._reply_logged = AsyncMock()
    bot._write_agents_plan_file = Mock(side_effect=OSError("workspace is read-only"))

    await bot._execute_pending_multi_plan(
        SimpleNamespace(effective_user=SimpleNamespace(id=123)), "456"
    )

    assert [path.name for path in root.iterdir()] == [".lightclaw-meta"]
    metadata = list((root / ".lightclaw-meta").glob("*.json"))
    assert len(metadata) == 1
    assert json.loads(metadata[0].read_text(encoding="utf-8"))["state"] == "undone"
    assert "No agent was started" in bot._reply_logged.await_args.args[1]
    assert not bot._active_run_ids_by_session
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
            "user_id": 123,
        },
    )
    bot._pending_multi_plan_by_session["456"]["review_delivered"] = True
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
        await bot._execute_pending_multi_plan(
            SimpleNamespace(effective_user=SimpleNamespace(id=123)), "456"
        )

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
@pytest.mark.parametrize("action", ["accept", "reject"])
async def test_result_decision_stays_bound_to_its_run_during_session_change(tmp_path, action):
    from core import artifacts

    bot = LightClawBot.__new__(LightClawBot)
    bot.jobs = JobStore(tmp_path / "jobs.db")
    bot._reply_logged = AsyncMock()
    roots = {run_id: tmp_path / run_id for run_id in ("old-run", "new-run")}
    receipt_paths = {}
    for run_id, root in roots.items():
        root.mkdir()
        (root / "result.txt").write_text("checkpoint\n")
        artifacts.initialize_artifact_repository(root, run_id)
        (root / "result.txt").write_text(f"reviewed {run_id}\n")
        review_dir = tmp_path / "receipts" / run_id
        bundle = artifacts.create_patch_bundle(root, review_dir, run_id=run_id)
        receipt_path = review_dir / "receipt.json"
        receipt_path.write_text(
            json.dumps({"run_id": run_id, "artifacts": [bundle["manifest"]]}),
            encoding="utf-8",
        )
        receipt_paths[run_id] = receipt_path
        bot.jobs.create_job(
            run_id=run_id, workspace=root, session_id="456", goal="fixture",
            approved_scope="fixture", risk_level="low", capability_profile="workspace-write",
            plan=[], status="queued",
        )
        bot.jobs.claim_next(run_id=run_id, worker_pid=999999)
        bot.jobs.finish(run_id, succeeded=True)
    new_root = roots["new-run"]
    new_head = artifacts._require_git(new_root, "rev-parse", "HEAD")
    new_index = (new_root / ".git" / "index").read_bytes()
    bot._last_run_ids_by_session = {"456": "old-run"}
    bot._last_run_workspaces_by_session = {"456": str(roots["old-run"])}
    bot._last_run_receipts_by_session = {"456": str(receipt_paths["old-run"])}
    lookup_started, lookup_release = threading.Event(), threading.Event()
    get_job = bot.jobs.get_job

    def delayed_lookup(run_id):
        lookup_started.set()
        assert lookup_release.wait(5), "fixture job lookup was not released"
        return get_job(run_id)

    bot.jobs.get_job = delayed_lookup
    decision = asyncio.create_task(
        getattr(bot, f"_{action}_last_run_result")(SimpleNamespace(), "456", "old-run")
    )
    try:
        assert await asyncio.to_thread(lookup_started.wait, 5)
        bot._last_run_ids_by_session["456"] = "new-run"
        bot._last_run_workspaces_by_session["456"] = str(new_root)
    finally:
        lookup_release.set()
    try:
        await decision
        assert artifacts._require_git(new_root, "rev-parse", "HEAD") == new_head
        assert (new_root / ".git" / "index").read_bytes() == new_index
        assert get_job("new-run")["status"] == "succeeded"
        assert get_job("old-run")["status"] == ("accepted" if action == "accept" else "rejected")
        assert artifacts._require_git(roots["old-run"], "diff", "--cached", "--name-only") == ""
    finally:
        bot.jobs.close()


@pytest.mark.asyncio
async def test_view_diff_sends_compact_summary_before_patch(tmp_path, monkeypatch):
    loop_thread = threading.get_ident()
    worker_threads = {}
    run_in_thread = approvals.await_thread_completion

    async def track_thread(function, *args, **kwargs):
        def record_thread(*inner_args, **inner_kwargs):
            worker_threads[function.__name__] = threading.get_ident()
            return function(*inner_args, **inner_kwargs)

        return await run_in_thread(record_thread, *args, **kwargs)

    monkeypatch.setattr(approvals, "await_thread_completion", track_thread)
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

    assert {"read_receipt", "open_patch", "read", "seek"} <= worker_threads.keys()
    assert all(thread_id != loop_thread for thread_id in worker_threads.values())
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
async def test_view_diff_closes_patch_fd_when_cancelled_while_opening(tmp_path, monkeypatch):
    patch_path = tmp_path / "changes.patch"
    patch_path.write_text("diff --git a/file b/file\n", encoding="utf-8")
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps({"run_id": "run-19", "artifacts": [str(patch_path)]}),
        encoding="utf-8",
    )
    open_started = threading.Event()
    open_release = threading.Event()
    opened_fds = []
    original_open = approvals.open_regular_file_at

    def delayed_open(root, relative):
        open_started.set()
        open_release.wait()
        result = original_open(root, relative)
        opened_fds.append(result[0])
        return result

    monkeypatch.setattr(approvals, "open_regular_file_at", delayed_open)
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(tmp_path))
    bot._last_run_receipts_by_session = {"456": str(receipt_path)}
    bot._reply_logged = AsyncMock()
    update = SimpleNamespace(message=SimpleNamespace(reply_document=AsyncMock()))

    task = asyncio.create_task(bot._send_last_run_diff(update, "456", "run-19"))
    try:
        assert await asyncio.to_thread(open_started.wait, 5)
        task.cancel()
    finally:
        open_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(opened_fds) == 1
    with pytest.raises(OSError):
        os.fstat(opened_fds[0])


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
    bot.config = SimpleNamespace(workspace_path=str(tmp_path))
    bot._last_run_receipts_by_session = {"456": str(receipt_path)}
    bot._reply_logged = AsyncMock()

    await bot._send_last_run_diff(SimpleNamespace(message=None), "456", "run-18")

    bot._reply_logged.assert_awaited_once_with(
        SimpleNamespace(message=None), "The local run receipt is unavailable."
    )


@pytest.mark.asyncio
async def test_history_diff_opens_a_persisted_run_for_its_chat(tmp_path):
    run_id = "run-0123456789abcdef"
    receipt_dir = tmp_path / ".lightclaw-meta" / "receipts" / run_id
    receipt_dir.mkdir(parents=True)
    patch_path = receipt_dir / "changes.patch"
    patch_path.write_text(
        "diff --git a/file.txt b/file.txt\n@@ -1 +1 @@\n-old\n+new\n",
        encoding="utf-8",
    )
    (receipt_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "diff_summary": "1 file changed, 1 insertion(+), 1 deletion(-)",
                "file_changes": [{"change": "modified", "path": "file.txt"}],
                "artifacts": [str(patch_path)],
            }
        ),
        encoding="utf-8",
    )
    jobs = SimpleNamespace(
        history_snapshot=Mock(return_value=77),
        list_jobs=Mock(
            return_value=[{"run_id": run_id, "status": "succeeded"}]
        )
    )
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(tmp_path))
    bot.jobs = jobs
    bot.is_update_allowed = lambda _update: True
    bot._session_scope_from_update = AsyncMock(return_value="chat-one")
    bot._last_run_receipts_by_session = {}
    bot._reply_logged = AsyncMock()
    message = SimpleNamespace(reply_document=AsyncMock())
    query = SimpleNamespace(
        data=f"lc:history:diff:1:77:{bot._run_action_token(run_id)}",
        message=message,
        answer=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=42),
    )

    await bot.handle_run_action(update, SimpleNamespace())

    jobs.list_jobs.assert_called_once_with(
        session_id="chat-one", limit=11, offset=10, snapshot_rowid=77
    )
    message.reply_document.assert_awaited_once()
    assert bot._reply_logged.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cursor", "offset", "uses_database_snapshot"),
    [("", 0, True), ("1:77:", 10, False)],
)
async def test_history_diff_button_cannot_read_another_chats_run(
    tmp_path, cursor, offset, uses_database_snapshot
):
    run_id = "run-0123456789abcdef"
    jobs = SimpleNamespace(
        history_snapshot=Mock(return_value=77), list_jobs=Mock(return_value=[])
    )
    bot = LightClawBot.__new__(LightClawBot)
    bot.jobs = jobs
    bot.is_update_allowed = lambda _update: True
    bot._session_scope_from_update = AsyncMock(return_value="chat-two")
    bot._reply_logged = AsyncMock()
    message = SimpleNamespace(reply_document=AsyncMock())
    query = SimpleNamespace(
        data=f"lc:history:diff:{cursor}{bot._run_action_token(run_id)}",
        message=message,
        answer=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=8),
        effective_chat=SimpleNamespace(id=43),
    )

    await bot.handle_run_action(update, SimpleNamespace())

    jobs.list_jobs.assert_called_once_with(
        session_id="chat-two", limit=11, offset=offset, snapshot_rowid=77
    )
    if uses_database_snapshot:
        jobs.history_snapshot.assert_called_once_with("chat-two")
    else:
        jobs.history_snapshot.assert_not_called()
    message.reply_document.assert_not_awaited()
    assert "no longer on the history page" in bot._reply_logged.await_args.args[1]


@pytest.mark.asyncio
async def test_history_page_callback_edits_message_in_place_for_current_chat():
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace()
    bot.jobs = SimpleNamespace(
        history_snapshot=Mock(return_value=123),
        list_jobs=Mock(
            return_value=[
                {
                    "run_id": "run-older",
                    "status": "succeeded",
                    "goal": "Older task",
                    "lanes": [],
                }
            ]
        )
    )
    bot.is_update_allowed = lambda _update: True
    bot._session_scope_from_update = AsyncMock(return_value="chat-two")
    bot._log_bot_message = Mock()
    bot._reply_logged = AsyncMock()
    query = SimpleNamespace(
        data="lc:history:page:1:123",
        message=SimpleNamespace(),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=8),
        effective_chat=SimpleNamespace(id=43),
    )

    await bot.handle_run_action(update, SimpleNamespace())

    bot.jobs.list_jobs.assert_called_once_with(
        session_id="chat-two", limit=11, offset=10, snapshot_rowid=123
    )
    query.edit_message_text.assert_awaited_once()
    assert "page 2" in query.edit_message_text.await_args.args[0]
    keyboard = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[-1][0].callback_data == "lc:history:page:0:123"
    bot._reply_logged.assert_not_awaited()
    bot._log_bot_message.assert_called_once()


@pytest.mark.asyncio
async def test_queued_history_cancel_is_private_only_and_refreshes_status(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job(
        workspace=tmp_path / "repo",
        session_id="chat-one",
        goal="queued task",
        approved_scope="fixture",
        risk_level="low",
        capability_profile="workspace-write",
        plan=[
            {
                "label": "delegation",
                "worker": "codex",
                "depends_on": [],
                "owned_paths": [],
                "idempotent": False,
                "resumable": False,
                "max_attempts": 1,
            }
        ],
        status="queued",
    )
    snapshot = store.history_snapshot("chat-one")
    bot = LightClawBot.__new__(LightClawBot)
    bot.jobs = store
    bot.is_update_allowed = lambda _update: True
    bot._session_scope_from_update = AsyncMock(return_value="chat-one")
    bot._reply_logged = AsyncMock()
    bot._log_bot_message = Mock()
    callback = bot._recent_runs_keyboard(
        [job], snapshot_rowid=snapshot, allow_cancel=True
    ).inline_keyboard[0][0].callback_data
    assert callback.startswith("lc:history:cancel:0:")
    assert len(callback.encode("utf-8")) <= 64
    assert bot._recent_runs_keyboard(
        [job], snapshot_rowid=snapshot, allow_cancel=False
    ) is None

    query = SimpleNamespace(
        data=callback,
        message=SimpleNamespace(),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=42, type="supergroup"),
    )
    await bot.handle_run_action(update, SimpleNamespace())
    assert store.get_job(job["run_id"])["status"] == "queued"
    assert "private chat only" in bot._reply_logged.await_args.args[1]

    bot._reply_logged.reset_mock()
    update.effective_chat.type = "private"
    await bot.handle_run_action(update, SimpleNamespace())
    canceled = store.get_job(job["run_id"])
    assert canceled["status"] == "canceled"
    assert canceled["lanes"][0]["status"] == "canceled"
    assert "canceled" in query.edit_message_text.await_args.args[0]
    refreshed_keyboard = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert refreshed_keyboard is None or all(
        button.text != "Cancel queued run"
        for row in refreshed_keyboard.inline_keyboard
        for button in row
    )
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_data",
    ["lc:history:page:1:9223372036854775808", "lc:history:page:01:123"],
)
async def test_history_page_callback_rejects_invalid_snapshot_and_page(callback_data):
    bot = LightClawBot.__new__(LightClawBot)
    bot.jobs = SimpleNamespace(
        history_snapshot=Mock(return_value=123), list_jobs=Mock(return_value=[])
    )
    bot.is_update_allowed = lambda _update: True
    bot._session_scope_from_update = AsyncMock(return_value="chat-one")
    bot._reply_logged = AsyncMock()
    query = SimpleNamespace(
        data=callback_data,
        message=SimpleNamespace(),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=8),
        effective_chat=SimpleNamespace(id=43),
    )

    await bot.handle_run_action(update, SimpleNamespace())

    bot.jobs.history_snapshot.assert_not_called()
    bot.jobs.list_jobs.assert_not_called()
    assert "history page is invalid" in bot._reply_logged.await_args.args[1]


@pytest.mark.asyncio
async def test_history_diff_refuses_symlinked_receipt_parent(tmp_path):
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    external_run = outside / "run-0123456789abcdef"
    external_run.mkdir(parents=True)
    outside_patch = external_run / "changes.patch"
    outside_patch.write_text("private patch", encoding="utf-8")
    (external_run / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "run-0123456789abcdef",
                "artifacts": [str(outside_patch)],
            }
        ),
        encoding="utf-8",
    )
    (root / ".lightclaw-meta").mkdir(parents=True)
    (root / ".lightclaw-meta" / "receipts").symlink_to(
        outside, target_is_directory=True
    )
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(root))
    bot._last_run_receipts_by_session = {}
    bot._reply_logged = AsyncMock()
    message = SimpleNamespace(reply_document=AsyncMock())

    await bot._send_last_run_diff(
        SimpleNamespace(message=message),
        "chat-one",
        "run-0123456789abcdef",
        receipt_value=Path(".lightclaw-meta/receipts/run-0123456789abcdef/receipt.json"),
    )

    message.reply_document.assert_not_awaited()
    bot._reply_logged.assert_awaited_once()
    assert bot._reply_logged.await_args.args[1] == "The local run receipt is unavailable."


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
