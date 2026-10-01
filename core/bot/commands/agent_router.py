"""/agent command handlers and multi-agent execution orchestration."""

from __future__ import annotations

import asyncio
import re
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ...markdown import _escape_html
from ..messaging import _TelegramHTMLChunker

_RUN_HISTORY_PAGE_SIZE = 10


class CommandsAgentRouterMixin:
    async def _plan_current_multi_request(
        self, session_id: str, *, requester_user_id: int, **kwargs
    ) -> tuple[dict[str, object] | None, str]:
        pending = self._set_pending_multi_plan(
            session_id, {"planning": True, "user_id": requester_user_id}
        )
        try:
            result = await self._plan_multi_agent_payload(**kwargs)
        except BaseException:
            if self._pending_multi_plan_by_session.get(session_id) is pending:
                self._clear_pending_multi_plan(session_id)
            raise
        if self._pending_multi_plan_by_session.get(session_id) is not pending:
            return None, ""
        self._clear_pending_multi_plan(session_id)
        planned, error = result
        if planned is not None:
            planned = {**planned, "user_id": requester_user_id}
        return planned, error

    async def _reply_multi_plan_preview(
        self,
        update: Update,
        preview: str,
        approval_id: str,
        approval_blocked: bool,
    ) -> None:
        session_id = await self._session_scope_from_update(update)
        pending = self._pending_multi_plan_by_session.get(session_id)
        if not pending or pending.get("approval_id") != approval_id:
            return
        chunker = _TelegramHTMLChunker(max_len=3000)
        chunker.feed(preview)
        chunks = chunker.finish()
        reply_markup = self._inline_plan_keyboard(
            approval_id,
            approval_blocked=approval_blocked,
        )
        try:
            for index, chunk in enumerate(chunks):
                sent = await self._reply_logged(
                    update,
                    chunk,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup if index == len(chunks) - 1 else None,
                )
        except BaseException:
            if self._pending_multi_plan_by_session.get(session_id) is pending:
                self._clear_pending_multi_plan(session_id)
            raise
        pending["review_message_id"] = getattr(sent, "message_id", None)
        pending["review_delivered"] = True

    async def _load_recent_runs_page(
        self, session_id: str, page: int, snapshot_rowid: int | None = None
    ) -> tuple[list[dict[str, object]], bool, int]:
        if snapshot_rowid is None:
            snapshot_rowid = await asyncio.to_thread(
                self.jobs.history_snapshot, session_id
            )
        jobs = await asyncio.to_thread(
            self.jobs.list_jobs,
            session_id=session_id,
            limit=_RUN_HISTORY_PAGE_SIZE + 1,
            offset=page * _RUN_HISTORY_PAGE_SIZE,
            snapshot_rowid=snapshot_rowid,
        )
        return (
            jobs[:_RUN_HISTORY_PAGE_SIZE],
            len(jobs) > _RUN_HISTORY_PAGE_SIZE,
            snapshot_rowid,
        )

    def _render_recent_runs(
        self, jobs: list[dict[str, object]], page: int = 0
    ) -> str:
        if not jobs:
            return f"<b>Run history · page {page + 1}</b>\nNo durable runs on this page."
        lines = [f"<b>Run history · page {page + 1}</b>"]
        for job in jobs:
            run_id = _escape_html(str(job.get("run_id") or "unknown"))
            status = _escape_html(str(job.get("status") or "unknown"))
            goal = re.sub(
                r"\s+", " ", self._visible_review_text(job.get("goal") or "")
            ).strip()
            if len(goal) > 120:
                goal = goal[:117].rstrip() + "..."
            lines.append(
                f"<code>{run_id}</code> · <b>{status}</b>\n"
                f"{_escape_html(goal or 'No task description')}"
            )
            lanes = job.get("lanes")
            lane_counts: dict[str, int] = {}
            if isinstance(lanes, list):
                for lane in lanes:
                    if isinstance(lane, dict):
                        lane_status = self._visible_review_text(
                            lane.get("status") or "unknown"
                        )
                        lane_counts[lane_status] = lane_counts.get(lane_status, 0) + 1
            if lane_counts:
                progress = " · ".join(
                    f"{count} {_escape_html(lane_status)}"
                    for lane_status, count in sorted(lane_counts.items())
                )
                lines.append(f"Lanes: {progress}")
        return "\n\n".join(lines)

    def _recent_runs_keyboard(
        self,
        jobs: list[dict[str, object]],
        page: int = 0,
        has_more: bool = False,
        snapshot_rowid: int | None = None,
    ) -> InlineKeyboardMarkup | None:
        buttons = []
        for job in jobs:
            run_id = str(job.get("run_id") or "")
            if (
                job.get("status") in {"succeeded", "failed", "accepted", "rejected"}
                and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id)
            ):
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"View diff · {run_id[-8:]}",
                            callback_data=(
                                f"lc:history:diff:{page}:{snapshot_rowid}:"
                                f"{self._run_action_token(run_id)}"
                                if snapshot_rowid is not None
                                else f"lc:history:diff:{page}:"
                                f"{self._run_action_token(run_id)}"
                            ),
                        )
                    ]
                )
        navigation = []
        if page:
            previous = f"lc:history:page:{page - 1}"
            if snapshot_rowid is not None:
                previous += f":{snapshot_rowid}"
            navigation.append(
                InlineKeyboardButton(
                    "Previous", callback_data=previous
                )
            )
        if has_more:
            next_page = f"lc:history:page:{page + 1}"
            if snapshot_rowid is not None:
                next_page += f":{snapshot_rowid}"
            navigation.append(
                InlineKeyboardButton(
                    "Next", callback_data=next_page
                )
            )
        if navigation:
            buttons.append(navigation)
        return InlineKeyboardMarkup(buttons) if buttons else None

    async def cmd_agent(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_update_allowed(update):
            return
        if self._privileged_rate_limited(update.effective_user.id, "agent", limit=12):
            await self._reply_logged(
                update,
                "⚠️ Too many privileged agent requests. Retry in about one minute.",
            )
            return

        session_id = await self._session_scope_from_update(update)
        args = context.args or []
        self._log_user_message(session_id, f"/agent {' '.join(args)}".strip())

        sub = args[0].lower() if args else "status"

        if sub in {"runs", "jobs"}:
            jobs, has_more, snapshot_rowid = await self._load_recent_runs_page(
                session_id, 0
            )
            await self._reply_logged(
                update,
                self._render_recent_runs(jobs),
                parse_mode=ParseMode.HTML,
                reply_markup=self._recent_runs_keyboard(
                    jobs, has_more=has_more, snapshot_rowid=snapshot_rowid
                ),
            )
            return

        if sub in {"status", "list", "ls"}:
            await self._reply_logged(
                update,
                self._render_agent_status(session_id),
                parse_mode=ParseMode.HTML,
            )
            return

        if sub in {"doctor", "diag", "check"}:
            report = await asyncio.to_thread(self._render_agent_doctor_report)
            await self._reply_logged(update, report, parse_mode=ParseMode.HTML)
            return

        if sub in {"use", "set", "on"}:
            if len(args) < 2:
                await self._reply_logged(
                    update,
                    "Usage: <code>/agent use &lt;codex|claude&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            agent = self._resolve_local_agent_name(args[1])
            if not agent:
                await self._reply_logged(
                    update,
                    "Unknown agent. Use one of: <code>codex</code>, "
                    "<code>claude</code>.",
                    parse_mode=ParseMode.HTML,
                )
                return

            available = self._available_local_agents()
            if agent not in available:
                installed = ", ".join(sorted(available.keys())) if available else "none"
                await self._reply_logged(
                    update,
                    f"⚠️ <code>{_escape_html(agent)}</code> is not installed.\n"
                    f"Installed: <code>{_escape_html(installed)}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            self._agent_mode_by_session[session_id] = agent
            await self._reply_logged(
                update,
                f"✅ Delegation mode enabled: <code>{_escape_html(agent)}</code>\n"
                "All normal chat messages in this chat will now run through this local agent.\n"
                "Disable with <code>/agent off</code>.",
                parse_mode=ParseMode.HTML,
            )
            return

        if sub in {"off", "disable", "stop"}:
            previous = self._agent_mode_by_session.pop(session_id, None)
            self._clear_pending_multi_plan(session_id)
            removed = await asyncio.to_thread(
                self.memory.delete_delegation_transcripts,
                session_id,
            )
            if previous:
                extra = (
                    f"\n🧹 Removed {removed} delegation transcript(s) from chat memory context."
                    if removed > 0
                    else ""
                )
                await self._reply_logged(
                    update,
                    f"✅ Delegation disabled (was <code>{_escape_html(previous)}</code>).{extra}",
                    parse_mode=ParseMode.HTML,
                )
            else:
                extra = (
                    f"\n🧹 Removed {removed} old delegation transcript(s) from chat memory context."
                    if removed > 0
                    else ""
                )
                await self._reply_logged(
                    update,
                    "Delegation mode is already disabled for this chat." + extra,
                )
            return

        if sub == "multi":
            parsed, parse_error = self._parse_multi_agent_args(args[1:])
            if parse_error:
                await self._reply_logged(
                    update,
                    parse_error,
                    parse_mode=ParseMode.HTML,
                )
                return

            action = str(parsed.get("action") or "")
            pending = self._get_pending_multi_plan(session_id)
            pending_for_action = pending or self._pending_multi_plan_by_session.get(
                session_id
            )

            if (
                action in {"confirm", "cancel", "edit"}
                and pending_for_action
                and not self._is_pending_requester(update, pending_for_action)
            ):
                await self._reply_logged(
                    update, "Only the requester can act on this plan."
                )
                return

            if action == "confirm":
                if not pending:
                    await self._reply_logged(
                        update,
                        "No pending multi-agent plan.\nStart one with <code>/agent multi &lt;goal&gt;</code>.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                approval_id = str(parsed.get("approval_id") or "")
                if not approval_id or not secrets.compare_digest(str(pending["approval_id"]), approval_id):
                    await self._reply_logged(
                        update,
                        "This confirmation does not identify the current reviewed plan. "
                        "Use its Approve button or copy /agent multi confirm <review-id> from the review.",
                    )
                    return
                await self._execute_pending_multi_plan(update, session_id)
                return

            if action == "cancel":
                cleared = self._clear_pending_multi_plan(session_id)
                if not cleared:
                    await self._reply_logged(
                        update,
                        "No pending multi-agent plan to cancel.",
                    )
                    return
                await self._reply_logged(update, "Cancelled pending multi-agent plan.")
                return

            if action == "edit":
                if not pending:
                    await self._reply_logged(
                        update,
                        "No pending multi-agent plan.\nStart one with <code>/agent multi &lt;goal&gt;</code>.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                feedback = str(parsed.get("feedback") or "").strip()
                goal = str(pending.get("goal") or "")
                explicit_specs = (
                    pending.get("explicit_specs")
                    if isinstance(pending.get("explicit_specs"), list)
                    else []
                )
                explicit_dependency_specs = (
                    pending.get("explicit_dependency_specs")
                    if isinstance(pending.get("explicit_dependency_specs"), dict)
                    else {}
                )
                preferred_agents = (
                    pending.get("preferred_agents")
                    if isinstance(pending.get("preferred_agents"), list)
                    else []
                )
                explicit_pairs: list[tuple[str, str]] = []
                for item in explicit_specs:
                    if not isinstance(item, (list, tuple)) or len(item) != 2:
                        continue
                    label = str(item[0]).strip()
                    agent = str(item[1]).strip()
                    if label and agent:
                        explicit_pairs.append((label, agent))
                available = self._available_local_agents()
                planned, plan_error = await self._plan_current_multi_request(
                    session_id,
                    requester_user_id=update.effective_user.id,
                    goal=goal,
                    available_agents=available,
                    explicit_specs=explicit_pairs,
                    explicit_dependency_specs={
                        str(k): [str(v) for v in values if isinstance(v, str)]
                        for k, values in explicit_dependency_specs.items()
                        if isinstance(k, str) and isinstance(values, list)
                    },
                    preferred_agents=[str(a) for a in preferred_agents if isinstance(a, str)],
                    feedback=feedback,
                )
                if planned is None:
                    return
                if plan_error:
                    await self._reply_logged(update, plan_error, parse_mode=ParseMode.HTML)
                    return
                pending_payload = self._set_pending_multi_plan(
                    session_id,
                    self._decorate_pending_plan(
                        {
                            **planned,
                            "feedback": feedback,
                        }
                    ),
                )
                preview_payload_obj = pending_payload.get("plan_payload")
                preview_payload = (
                    preview_payload_obj
                    if isinstance(preview_payload_obj, dict)
                    else {}
                )
                preview_warnings_obj = pending_payload.get("warnings")
                preview_warnings = (
                    preview_warnings_obj
                    if isinstance(preview_warnings_obj, list)
                    else []
                )
                preview = self._render_multi_plan_preview(
                    goal=str(pending_payload.get("goal") or ""),
                    workers=list(pending_payload.get("workers") or []),
                    plan_payload=preview_payload,
                    warnings=[str(item) for item in preview_warnings],
                    include_confirm_hint=not bool(pending_payload["review"]["approval_blocked"]),
                    approval_id=str(pending_payload["approval_id"]),
                )
                preview += "\n\n" + self._render_plan_review(pending_payload)
                await self._reply_multi_plan_preview(
                    update,
                    preview,
                    str(pending_payload["approval_id"]),
                    bool(pending_payload["review"]["approval_blocked"]),
                )
                return

            goal = str(parsed.get("goal") or "").strip()
            explicit_specs_obj = parsed.get("explicit_specs")
            explicit_specs = explicit_specs_obj if isinstance(explicit_specs_obj, list) else []
            explicit_dependency_specs_obj = parsed.get("explicit_dependency_specs")
            explicit_dependency_specs = (
                explicit_dependency_specs_obj
                if isinstance(explicit_dependency_specs_obj, dict)
                else {}
            )
            preferred_agents_obj = parsed.get("preferred_agents")
            preferred_agents = preferred_agents_obj if isinstance(preferred_agents_obj, list) else []
            explicit_pairs: list[tuple[str, str]] = []
            for item in explicit_specs:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    continue
                label = str(item[0]).strip()
                agent = str(item[1]).strip()
                if label and agent:
                    explicit_pairs.append((label, agent))

            available = self._available_local_agents()
            planned, plan_error = await self._plan_current_multi_request(
                session_id,
                requester_user_id=update.effective_user.id,
                goal=goal,
                available_agents=available,
                explicit_specs=explicit_pairs,
                explicit_dependency_specs={
                    str(k): [str(v) for v in values if isinstance(v, str)]
                    for k, values in explicit_dependency_specs.items()
                    if isinstance(k, str) and isinstance(values, list)
                },
                preferred_agents=[str(a) for a in preferred_agents if isinstance(a, str)],
            )
            if planned is None:
                return
            if plan_error:
                await self._reply_logged(update, plan_error, parse_mode=ParseMode.HTML)
                return

            pending_payload = self._set_pending_multi_plan(
                session_id,
                self._decorate_pending_plan(planned),
            )
            preview_payload_obj = pending_payload.get("plan_payload")
            preview_payload = (
                preview_payload_obj if isinstance(preview_payload_obj, dict) else {}
            )
            preview_warnings_obj = pending_payload.get("warnings")
            preview_warnings = (
                preview_warnings_obj if isinstance(preview_warnings_obj, list) else []
            )
            preview = self._render_multi_plan_preview(
                goal=str(pending_payload.get("goal") or ""),
                workers=list(pending_payload.get("workers") or []),
                plan_payload=preview_payload,
                warnings=[str(item) for item in preview_warnings],
                include_confirm_hint=not bool(pending_payload["review"]["approval_blocked"]),
                approval_id=str(pending_payload["approval_id"]),
            )
            preview += "\n\n" + self._render_plan_review(pending_payload)
            await self._reply_multi_plan_preview(
                update,
                preview,
                str(pending_payload["approval_id"]),
                bool(pending_payload["review"]["approval_blocked"]),
            )

            if self.config.local_agent_multi_auto_continue:
                await self._reply_logged(
                    update,
                    "Auto-continue is ignored for safety. Use the explicit Approve button.",
                )
            return

        if sub in {"observe", "trusted"}:
            profile = "observe" if sub == "observe" else "trusted-command"
            if sub == "trusted" and len(args) >= 2 and args[1].lower() in {"confirm", "discard"}:
                pending = self._pending_trusted_agent_run_by_session.get(session_id)
                if not pending or self._pending_confirmation_expired(pending):
                    self._pending_trusted_agent_run_by_session.pop(session_id, None)
                    await self._reply_logged(
                        update,
                        "No pending trusted run. Start with "
                        "<code>/agent trusted &lt;agent&gt; &lt;task&gt;</code>.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                if not self._is_pending_requester(update, pending):
                    await self._reply_logged(
                        update,
                        "Only the Telegram user who requested this trusted run can confirm it.",
                    )
                    return
                if (
                    len(args) != 3
                    or not re.fullmatch(r"[0-9a-f]{16}", args[2])
                    or not secrets.compare_digest(str(pending.get("approval_id") or ""), args[2])
                    or not pending.get("reviewed")
                ):
                    await self._reply_logged(
                        update,
                        "Review the latest complete request, then use its Approve host run "
                        "or Discard button, or /agent trusted confirm <review-id>.",
                    )
                    return
                self._pending_trusted_agent_run_by_session.pop(session_id, None)
                if args[1].lower() == "discard":
                    await self._reply_logged(update, "Discarded trusted run. Nothing was executed.")
                    return
                agent = str(pending.get("agent") or "")
                task = str(pending.get("task") or "")
                await self._execute_one_shot_delegation(
                    update,
                    session_id=session_id,
                    agent=agent,
                    task=task,
                    capability_profile=profile,
                )
                return

            if len(args) < 3:
                await self._reply_logged(
                    update,
                    f"Usage: <code>/agent {sub} &lt;codex|claude&gt; &lt;task&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return
            agent = self._resolve_local_agent_name(args[1])
            task = " ".join(args[2:]).strip()
            if not agent or not task:
                await self._reply_logged(
                    update,
                    "A supported agent and non-empty task are required.",
                )
                return

            if sub == "trusted":
                now = time.time()
                task = self._visible_review_text(task)
                approval_id = secrets.token_hex(8)
                pending = {
                    "user_id": update.effective_user.id,
                    "approval_id": approval_id,
                    "reviewed": False,
                    "agent": agent,
                    "task": task,
                    "expires_at": now + 90,
                    "expires_monotonic": time.monotonic() + 90,
                }
                self._pending_trusted_agent_run_by_session[session_id] = pending
                preview = (
                    "⚠️ <b>Trusted host execution requested.</b>\n"
                    "This disables the coding agent sandbox for one run and may affect "
                    "files or processes outside the task workspace.\n\n"
                    f"<b>Agent:</b> <code>{_escape_html(agent)}</code>\n"
                    f"<b>Task:</b>\n{_escape_html(task)}\n\n"
                    "Only the requester can approve or discard within 90 seconds. "
                    "Review every part before approving.\n"
                    f"<code>/agent trusted confirm {approval_id}</code>\n"
                    f"<code>/agent trusted discard {approval_id}</code>"
                )
                chunker = _TelegramHTMLChunker(max_len=3000)
                chunker.feed(preview)
                chunks = chunker.finish()
                try:
                    for index, chunk in enumerate(chunks):
                        await self._reply_logged(
                            update,
                            chunk,
                            parse_mode=ParseMode.HTML,
                            reply_markup=(
                                self._inline_trusted_keyboard(approval_id)
                                if index == len(chunks) - 1 else None
                            ),
                        )
                except BaseException:
                    if self._pending_trusted_agent_run_by_session.get(session_id) is pending:
                        self._pending_trusted_agent_run_by_session.pop(session_id, None)
                    raise
                pending["reviewed"] = True
                return

            await self._execute_one_shot_delegation(
                update,
                session_id=session_id,
                agent=agent,
                task=task,
                capability_profile=profile,
            )
            return

        # One-shot convenience: /agent codex <task...>
        direct_agent = self._resolve_local_agent_name(sub)
        if direct_agent:
            task = " ".join(args[1:]).strip()
            if not task:
                await self._reply_logged(
                    update,
                    f"Usage: <code>/agent {_escape_html(direct_agent)} &lt;task&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return
            await self._execute_one_shot_delegation(
                update,
                session_id=session_id,
                agent=direct_agent,
                task=task,
                capability_profile=self.config.local_agent_capability_profile,
            )
            return

        if sub == "run":
            if len(args) < 2:
                await self._reply_logged(
                    update,
                    "Usage: <code>/agent run &lt;task&gt;</code> or "
                    "<code>/agent run &lt;agent&gt; &lt;task&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            requested_agent = self._resolve_local_agent_name(args[1])
            if requested_agent and len(args) >= 3:
                agent = requested_agent
                task = " ".join(args[2:]).strip()
            else:
                agent = self._agent_mode_by_session.get(session_id)
                task = " ".join(args[1:]).strip()

            if not agent:
                await self._reply_logged(
                    update,
                    "No active local agent for this chat.\n"
                    "Set one first: <code>/agent use codex</code> "
                    "(or claude).",
                    parse_mode=ParseMode.HTML,
                )
                return
            if not task:
                await self._reply_logged(
                    update,
                    "Task is required.",
                )
                return

            await self._execute_one_shot_delegation(
                update,
                session_id=session_id,
                agent=agent,
                task=task,
                capability_profile=self.config.local_agent_capability_profile,
            )
            return

        await self._reply_logged(
            update,
            "Unknown /agent subcommand.\n\n" + self._agent_usage_text(),
            parse_mode=ParseMode.HTML,
        )

    async def _execute_one_shot_delegation(
        self,
        update: Update,
        *,
        session_id: str,
        agent: str,
        task: str,
        capability_profile: str,
    ) -> None:
        async with self._memory_request_guard(session_id) as clear_event:
            progress = await self._reply_logged(
                update,
                f"🤖 Delegating to <code>{_escape_html(agent)}</code> "
                f"with <code>{_escape_html(capability_profile)}</code> capability...",
                parse_mode=ParseMode.HTML,
            )

            async def _delegation_progress_update(text: str):
                try:
                    run_id = self._active_run_ids_by_session.get(session_id)
                    await progress.edit_text(
                        text,
                        reply_markup=(
                            self._inline_cancel_keyboard(run_id) if run_id else None
                        ),
                    )
                except Exception:
                    pass

            result_text = await self._run_local_agent_task(
                session_id,
                agent,
                task,
                progress_cb=_delegation_progress_update,
                capability_profile=capability_profile,
                requester_user_id=update.effective_user.id,
            )
            if not clear_event.is_set():
                request_entry = (
                    "[delegation-request]\n"
                    "mode: single\n"
                    f"capability: {capability_profile}\n"
                    f"agent: {agent}\n"
                    f"task: {task}"
                )
                await self._ingest_memory(
                    "user", request_entry, session_id, clear_event=clear_event
                )
                memory_entry = self._build_single_delegation_memory_entry(
                    agent=agent,
                    task=task,
                    result_text=result_text,
                )
                await self._ingest_memory(
                    "assistant", memory_entry, session_id, clear_event=clear_event
                )
                if not self._llm_backoff_active():
                    self._create_background_task(self.maybe_summarize(session_id))
            await self._send_response(progress, update, result_text)
