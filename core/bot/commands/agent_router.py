"""/agent command handlers and multi-agent execution orchestration."""

from __future__ import annotations

import asyncio
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ...markdown import _escape_html


class CommandsAgentRouterMixin:
    async def cmd_agent(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return
        if self._privileged_rate_limited(update.effective_user.id, "agent", limit=12):
            await self._reply_logged(
                update,
                "⚠️ Too many privileged agent requests. Retry in about one minute.",
            )
            return

        session_id = self._session_id_from_update(update)
        args = context.args or []
        self._log_user_message(session_id, f"/agent {' '.join(args)}".strip())

        sub = args[0].lower() if args else "status"
        if sub in {"list", "ls"}:
            sub = "status"

        if sub == "status":
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

            if action == "confirm":
                if not pending:
                    await self._reply_logged(
                        update,
                        "No pending multi-agent plan.\nStart one with <code>/agent multi &lt;goal&gt;</code>.",
                        parse_mode=ParseMode.HTML,
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
                planned, plan_error = await self._plan_multi_agent_payload(
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
                    include_confirm_hint=True,
                )
                preview += "\n\n" + self._render_plan_review(pending_payload)
                await self._reply_logged(
                    update,
                    preview,
                    parse_mode=ParseMode.HTML,
                    reply_markup=self._inline_plan_keyboard(),
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
            planned, plan_error = await self._plan_multi_agent_payload(
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
                include_confirm_hint=True,
            )
            preview += "\n\n" + self._render_plan_review(pending_payload)
            await self._reply_logged(
                update,
                preview,
                parse_mode=ParseMode.HTML,
                reply_markup=self._inline_plan_keyboard(),
            )

            if self.config.local_agent_multi_auto_continue:
                await self._reply_logged(
                    update,
                    "Auto-continue is ignored for safety. Use the explicit Approve button.",
                )
            return

        if sub in {"observe", "trusted"}:
            profile = "observe" if sub == "observe" else "trusted-command"
            if sub == "trusted" and len(args) >= 2 and args[1].lower() == "confirm":
                pending = self._pending_trusted_agent_run_by_session.get(session_id)
                if not pending or float(pending.get("expires_at") or 0) < time.time():
                    self._pending_trusted_agent_run_by_session.pop(session_id, None)
                    await self._reply_logged(
                        update,
                        "No pending trusted run. Start with "
                        "<code>/agent trusted &lt;agent&gt; &lt;task&gt;</code>.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                self._pending_trusted_agent_run_by_session.pop(session_id, None)
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
                self._pending_trusted_agent_run_by_session[session_id] = {
                    "agent": agent,
                    "task": task,
                    "expires_at": time.time() + 90,
                }
                await self._reply_logged(
                    update,
                    "⚠️ <b>Trusted host execution requested.</b>\n"
                    "This disables the coding agent sandbox for one run and may affect "
                    "files or processes outside the task workspace.\n\n"
                    "Confirm within 90 seconds with <code>/agent trusted confirm</code>.",
                    parse_mode=ParseMode.HTML,
                )
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
            progress = await self._reply_logged(
                update,
                f"🤖 Delegating to <code>{_escape_html(direct_agent)}</code>...",
                parse_mode=ParseMode.HTML,
            )

            async def _delegation_progress_update(text: str):
                try:
                    await progress.edit_text(text)
                except Exception:
                    pass

            result_text = await self._run_local_agent_task(
                session_id,
                direct_agent,
                task,
                progress_cb=_delegation_progress_update,
            )
            request_entry = (
                "[delegation-request]\n"
                "mode: single\n"
                f"agent: {direct_agent}\n"
                f"task: {task}"
            )
            self.memory.ingest("user", request_entry, session_id)
            memory_entry = self._build_single_delegation_memory_entry(
                agent=direct_agent,
                task=task,
                result_text=result_text,
            )
            self.memory.ingest("assistant", memory_entry, session_id)
            if not self._llm_backoff_active():
                asyncio.create_task(self.maybe_summarize(session_id))
            await self._send_response(progress, update, result_text)
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

            progress = await self._reply_logged(
                update,
                f"🤖 Delegating to <code>{_escape_html(agent)}</code>...",
                parse_mode=ParseMode.HTML,
            )

            async def _delegation_progress_update(text: str):
                try:
                    await progress.edit_text(text)
                except Exception:
                    pass

            result_text = await self._run_local_agent_task(
                session_id,
                agent,
                task,
                progress_cb=_delegation_progress_update,
            )
            request_entry = (
                "[delegation-request]\n"
                "mode: single\n"
                f"agent: {agent}\n"
                f"task: {task}"
            )
            self.memory.ingest("user", request_entry, session_id)
            memory_entry = self._build_single_delegation_memory_entry(
                agent=agent,
                task=task,
                result_text=result_text,
            )
            self.memory.ingest("assistant", memory_entry, session_id)
            if not self._llm_backoff_active():
                asyncio.create_task(self.maybe_summarize(session_id))
            await self._send_response(progress, update, result_text)
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
        progress = await self._reply_logged(
            update,
            f"🤖 Delegating to <code>{_escape_html(agent)}</code> "
            f"with <code>{_escape_html(capability_profile)}</code> capability...",
            parse_mode=ParseMode.HTML,
        )

        async def _delegation_progress_update(text: str):
            try:
                await progress.edit_text(text)
            except Exception:
                pass

        result_text = await self._run_local_agent_task(
            session_id,
            agent,
            task,
            progress_cb=_delegation_progress_update,
            capability_profile=capability_profile,
        )
        request_entry = (
            "[delegation-request]\n"
            "mode: single\n"
            f"capability: {capability_profile}\n"
            f"agent: {agent}\n"
            f"task: {task}"
        )
        self.memory.ingest("user", request_entry, session_id)
        memory_entry = self._build_single_delegation_memory_entry(
            agent=agent,
            task=task,
            result_text=result_text,
        )
        self.memory.ingest("assistant", memory_entry, session_id)
        if not self._llm_backoff_active():
            asyncio.create_task(self.maybe_summarize(session_id))
        await self._send_response(progress, update, result_text)
