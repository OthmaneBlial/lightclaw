"""/agent command handlers and multi-agent execution orchestration."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from skills import SkillError

from ...logging_setup import log
from ...markdown import _escape_html, markdown_to_telegram_html
from ...personality import build_system_prompt, runtime_root_from_workspace

class CommandsAgentMixin:
    async def cmd_agent(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        session_id = str(update.effective_chat.id) if update.effective_chat else "unknown"
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
                    preferred_agents=[str(a) for a in preferred_agents if isinstance(a, str)],
                    feedback=feedback,
                )
                if plan_error:
                    await self._reply_logged(update, plan_error, parse_mode=ParseMode.HTML)
                    return
                pending_payload = self._set_pending_multi_plan(
                    session_id,
                    {
                        **planned,
                        "feedback": feedback,
                    },
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
                await self._reply_logged(update, preview, parse_mode=ParseMode.HTML)
                return

            goal = str(parsed.get("goal") or "").strip()
            explicit_specs_obj = parsed.get("explicit_specs")
            explicit_specs = explicit_specs_obj if isinstance(explicit_specs_obj, list) else []
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
                preferred_agents=[str(a) for a in preferred_agents if isinstance(a, str)],
            )
            if plan_error:
                await self._reply_logged(update, plan_error, parse_mode=ParseMode.HTML)
                return

            pending_payload = self._set_pending_multi_plan(session_id, planned)
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
                include_confirm_hint=not bool(self.config.local_agent_multi_auto_continue),
            )
            await self._reply_logged(update, preview, parse_mode=ParseMode.HTML)

            if self.config.local_agent_multi_auto_continue:
                await self._reply_logged(
                    update,
                    "Auto-continue is enabled. Executing multi-agent run now...",
                )
                await self._execute_pending_multi_plan(update, session_id)
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


    async def _execute_pending_multi_plan(self, update: Update, session_id: str):
        pending = self._get_pending_multi_plan(session_id)
        if not pending:
            await self._reply_logged(
                update,
                "No pending multi-agent plan.\nStart one with <code>/agent multi &lt;goal&gt;</code>.",
                parse_mode=ParseMode.HTML,
            )
            return

        goal = str(pending.get("goal") or "").strip()
        workers_obj = pending.get("workers")
        workers = workers_obj if isinstance(workers_obj, list) else []
        plan_payload_obj = pending.get("plan_payload")
        plan_payload = plan_payload_obj if isinstance(plan_payload_obj, dict) else {}

        resolved_workers: list[tuple[str, str]] = []
        for item in workers:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            label = str(item[0]).strip()
            agent = str(item[1]).strip()
            if not label or not agent:
                continue
            resolved_workers.append((label, agent))

        if not goal or len(resolved_workers) < 2 or not plan_payload:
            self._clear_pending_multi_plan(session_id)
            await self._reply_logged(
                update,
                "Pending multi-agent plan is invalid or expired. Please create a new one.",
            )
            return

        self._clear_pending_multi_plan(session_id)
        await self._execute_multi_agent_plan(
            update=update,
            session_id=session_id,
            goal=goal,
            workers=resolved_workers,
            plan_payload=plan_payload,
        )


    async def _execute_multi_agent_plan(
        self,
        update: Update,
        session_id: str,
        goal: str,
        workers: list[tuple[str, str]],
        plan_payload: dict[str, object],
    ):
        multi_workspace = await asyncio.to_thread(self._create_task_workspace, goal)
        multi_workspace_label = self._workspace_rel_label(multi_workspace)
        agents_path = await asyncio.to_thread(
            self._write_agents_plan_file, multi_workspace, plan_payload
        )
        loaded_payload = await asyncio.to_thread(
            self._load_agents_plan_file, multi_workspace
        )
        if loaded_payload:
            plan_payload = loaded_payload

        handoff_dir = multi_workspace / "handoff"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        before_multi = await asyncio.to_thread(
            self._snapshot_workspace_state, multi_workspace
        )

        plan_lines = ["🤖 <b>Multi-Agent Execution</b>", ""]
        plan_lines.append(f"<b>Goal:</b> {_escape_html(goal)}")
        plan_lines.append(
            f"<b>Task workspace:</b> <code>{_escape_html(multi_workspace_label)}</code>"
        )
        plan_lines.append(f"<b>AGENTS.md:</b> <code>{_escape_html(agents_path.name)}</code>")
        plan_lines.append("")
        plan_lines.append("Starting dependency-phased parallel workers...")
        await self._reply_logged(update, "\n".join(plan_lines), parse_mode=ParseMode.HTML)

        worker_msgs = []
        for index, (label, agent) in enumerate(workers):
            tag = self._multi_agent_tag(label, agent, index)
            worker_msg = await self._reply_logged(
                update,
                f"<code>{_escape_html(tag)}</code>\nQueued...",
                parse_mode=ParseMode.HTML,
            )
            worker_msgs.append(worker_msg)

        def _is_success_result(worker_result: str) -> bool:
            return "✅ Finished in " in (worker_result or "")

        worker_contracts = plan_payload.get("workers")
        contract_list = worker_contracts if isinstance(worker_contracts, list) else []
        workers_by_label = {label: agent for label, agent in workers}
        worker_contract_by_label: dict[str, dict[str, object]] = {
            label: {} for label, _ in workers
        }
        for contract in contract_list:
            if not isinstance(contract, dict):
                continue
            label = str(contract.get("label") or "").strip()
            if not label or label not in workers_by_label:
                continue
            worker_contract_by_label[label] = contract

        dependency_map: dict[str, list[str]] = {}
        unknown_dependency_map: dict[str, list[str]] = {}
        for label, _ in workers:
            contract = worker_contract_by_label.get(label, {})
            depends_obj = contract.get("depends_on")
            raw_deps = (
                [str(dep).strip() for dep in depends_obj]
                if isinstance(depends_obj, list)
                else []
            )
            valid_deps: list[str] = []
            unknown_deps: list[str] = []
            seen: set[str] = set()
            for dep in raw_deps:
                if not dep or dep == label or dep in seen:
                    continue
                seen.add(dep)
                if dep in workers_by_label:
                    valid_deps.append(dep)
                else:
                    unknown_deps.append(dep)
            dependency_map[label] = valid_deps
            unknown_dependency_map[label] = unknown_deps
            contract["depends_on"] = valid_deps

        async def _run_worker(index: int, label: str, agent: str, progress_msg):
            tag = self._multi_agent_tag(label, agent, index)
            worker_contract = worker_contract_by_label.get(label, {})
            worker_task = self._build_multi_agent_worker_task(
                label=label,
                goal=goal,
                workers=workers,
                worker_plan=worker_contract,
                task_workspace_label=multi_workspace_label,
            )

            async def _worker_progress_update(text: str):
                try:
                    await progress_msg.edit_text(f"{tag}\n{text}")
                except Exception:
                    pass

            try:
                result = await self._run_local_agent_task(
                    session_id=session_id,
                    agent=agent,
                    task=worker_task,
                    progress_cb=_worker_progress_update,
                    include_workspace_delta=False,
                    workspace_dir=multi_workspace,
                )
                ok = _is_success_result(result)
                status_text = "✅ Worker completed." if ok else "⚠️ Worker finished with issues."
                try:
                    await progress_msg.edit_text(f"{tag}\n{status_text}")
                except Exception:
                    pass
                return (label, agent, result, ok)
            except Exception as e:
                fail_result = f"⚠️ Worker failed: {e}"
                try:
                    await progress_msg.edit_text(f"{tag}\n{fail_result}")
                except Exception:
                    pass
                return (label, agent, fail_result, False)

        pending = set(workers_by_label.keys())
        completed_ok: set[str] = set()
        failed: set[str] = set()
        results_by_label: dict[str, object] = {}
        index_by_label = {label: idx for idx, (label, _) in enumerate(workers)}
        wait_status_by_label: dict[str, str] = {}

        async def _set_worker_status(label: str, status_text: str):
            if wait_status_by_label.get(label) == status_text:
                return
            wait_status_by_label[label] = status_text
            msg = worker_msgs[index_by_label[label]]
            tag = self._multi_agent_tag(
                label,
                workers_by_label[label],
                index_by_label[label],
            )
            try:
                await msg.edit_text(f"{tag}\n{status_text}")
            except Exception:
                pass

        for label in list(pending):
            unknown_deps = unknown_dependency_map.get(label) or []
            if not unknown_deps:
                continue
            pending.discard(label)
            failed.add(label)
            reason = ", ".join(unknown_deps)
            skip_text = f"⚠️ Skipped because AGENTS.md references unknown dependency: {reason}"
            results_by_label[label] = skip_text
            await _set_worker_status(label, skip_text)

        while pending:
            blocked_now: list[str] = []
            for label in list(pending):
                dep_list = dependency_map.get(label) or []
                if any(dep in failed for dep in dep_list):
                    blocked_now.append(label)

            for label in blocked_now:
                pending.discard(label)
                failed.add(label)
                dep_list = dependency_map.get(label) or []
                reason = ", ".join(d for d in dep_list if d in failed) or "failed dependency"
                skip_text = f"⚠️ Skipped because dependency failed: {reason}"
                results_by_label[label] = skip_text
                await _set_worker_status(label, skip_text)

            ready: list[str] = []
            for label in list(pending):
                dep_list = dependency_map.get(label) or []
                unresolved = [dep for dep in dep_list if dep not in completed_ok]
                if not unresolved:
                    ready.append(label)
                    continue
                resolved_count = len(dep_list) - len(unresolved)
                wait_text = (
                    f"⏳ Waiting for dependencies ({resolved_count}/{len(dep_list)} ready): "
                    + ", ".join(unresolved)
                )
                await _set_worker_status(label, wait_text)

            if not ready:
                for label in list(pending):
                    pending.discard(label)
                    failed.add(label)
                    skip_text = "⚠️ Skipped due to unresolved dependency cycle in AGENTS.md."
                    results_by_label[label] = skip_text
                    await _set_worker_status(label, skip_text)
                break

            run_tasks = [
                _run_worker(
                    index_by_label[label],
                    label,
                    workers_by_label[label],
                    worker_msgs[index_by_label[label]],
                )
                for label in ready
            ]
            phase_results = await asyncio.gather(*run_tasks)

            for label in ready:
                pending.discard(label)

            for result in phase_results:
                label, _agent, worker_result, ok = result
                results_by_label[label] = worker_result
                if ok:
                    completed_ok.add(label)
                else:
                    failed.add(label)

        after_multi = await asyncio.to_thread(
            self._snapshot_workspace_state, multi_workspace
        )
        multi_delta = self._summarize_workspace_delta(before_multi, after_multi)

        final_lines = [
            "🤖 Multi-agent run finished.",
            f"Goal: {goal}",
            f"Task workspace: {multi_workspace_label}",
            "",
        ]

        for index, (label, agent) in enumerate(workers):
            tag = self._multi_agent_tag(label, agent, index)
            worker_result = str(results_by_label.get(label, "⚠️ Worker did not produce output."))
            final_lines.append(f"{tag}")
            final_lines.append(worker_result)
            final_lines.append("")

        final_lines.append(multi_delta)

        request_entry = (
            "[delegation-request]\n"
            "mode: multi\n"
            f"goal: {goal}\n"
            f"workers: {', '.join(f'{label}={agent}' for label, agent in workers)}"
        )
        self.memory.ingest("user", request_entry, session_id)
        memory_entry = self._build_multi_delegation_memory_entry(
            goal=goal,
            workspace_label=multi_workspace_label,
            workers=workers,
            results_by_label=results_by_label,
        )
        self.memory.ingest("assistant", memory_entry, session_id)
        if not self._llm_backoff_active():
            asyncio.create_task(self.maybe_summarize(session_id))

        await self._send_response(None, update, "\n".join(final_lines).strip())

