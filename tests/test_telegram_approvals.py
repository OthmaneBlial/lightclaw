from __future__ import annotations

import asyncio
import json
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.bot import LightClawBot
from core.jobs import JobStore


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
        for row in bot._inline_result_keyboard(run_id, ["builder"]).inline_keyboard
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
        f"lc:run:retry:{run_token}:builder",
    } <= set(result_data)
    cancel_data = [
        button.callback_data
        for row in bot._inline_cancel_keyboard(run_id).inline_keyboard
        for button in row
    ]
    assert cancel_data == [f"lc:run:cancel:{run_token}"]


@pytest.mark.asyncio
async def test_voice_transcription_waits_for_explicit_approval(monkeypatch):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(groq_api_key="fixture")
    bot.is_update_allowed = lambda _update: True
    bot._pending_voice_goal_by_session = {}
    bot._reply_logged = AsyncMock()
    bot._process_user_message = AsyncMock()
    monkeypatch.setattr("core.bot.handlers.transcribe_voice", AsyncMock(return_value="Build the fixture"))

    voice_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"audio")))
    voice = SimpleNamespace(get_file=AsyncMock(return_value=voice_file))
    message = SimpleNamespace(voice=voice, caption="", reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456, type="private"),
        message=message,
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))

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
async def test_cancelled_multi_plan_stops_workers_and_releases_durable_job(tmp_path):
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
    worker_started = asyncio.Event()
    worker_cleaned = asyncio.Event()
    never = asyncio.Event()
    run_id_holder: list[str] = []
    heartbeat_holder: list[asyncio.Task] = []

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
    patch_path.write_text("diff --git a/file b/file\n", encoding="utf-8")
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "run_id": "run-18",
                "diff_summary": "large file-by-file stat\n3 files changed, 7 insertions(+), 2 deletions(-)",
                "file_changes": [
                    {"change": "modified", "path": "src/main.py"},
                    {"change": "added", "path": "tests/test_main.py"},
                    {"change": "deleted", "path": "old.txt"},
                ],
                "artifacts": [str(patch_path)],
            }
        ),
        encoding="utf-8",
    )
    events = []

    async def send_summary(_update, text):
        events.append(("summary", text))

    async def send_patch(**_kwargs):
        events.append(("patch", ""))

    bot = LightClawBot.__new__(LightClawBot)
    bot._last_run_receipts_by_session = {"456": str(receipt_path)}
    bot._reply_logged = AsyncMock(side_effect=send_summary)
    message = SimpleNamespace(reply_document=AsyncMock(side_effect=send_patch))
    update = SimpleNamespace(message=message)

    await bot._send_last_run_diff(update, "456", "run-18")

    assert [kind for kind, _ in events] == ["summary", "patch"]
    assert "3 files changed, 7 insertions(+), 2 deletions(-)" in events[0][1]
    assert "src/main.py" in events[0][1]
    assert "nothing has been accepted or pushed" in events[0][1]
    message.reply_document.assert_awaited_once()


@pytest.mark.asyncio
async def test_long_result_is_private_file_artifact_not_chat_wall(tmp_path):
    bot = LightClawBot.__new__(LightClawBot)
    bot.config = SimpleNamespace(workspace_path=str(tmp_path / "workspace"))
    message = SimpleNamespace(reply_document=AsyncMock(), reply_text=AsyncMock())
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
