"""/agent command handlers and multi-agent execution orchestration."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.constants import ParseMode

from ...artifacts import ArtifactError, create_patch_bundle, initialize_artifact_repository
from ...fs import open_directory_at
from ...jobs import JobConflictError, JobStateError
from ...logging_setup import log
from ...markdown import _escape_html
from ...receipts import write_receipt
from ..delegation.workspace import await_thread_completion


def _multi_worker_dependencies(
    workers: list[tuple[str, str]], plan_payload: dict[str, object]
) -> tuple[
    dict[str, dict[str, object]],
    dict[str, list[str]],
    dict[str, list[str]],
]:
    worker_labels = {label for label, _agent in workers}
    contracts = plan_payload.get("workers")
    contracts = contracts if isinstance(contracts, list) else []
    contract_by_label: dict[str, dict[str, object]] = {
        label: {} for label, _agent in workers
    }
    for contract in contracts:
        if not isinstance(contract, dict):
            continue
        label = str(contract.get("label") or "").strip()
        if label and label in worker_labels:
            contract_by_label[label] = contract

    dependencies: dict[str, list[str]] = {}
    unknown_dependencies: dict[str, list[str]] = {}
    for label, _agent in workers:
        contract = contract_by_label[label]
        raw_dependencies = contract.get("depends_on")
        raw_dependencies = (
            [str(dep).strip() for dep in raw_dependencies]
            if isinstance(raw_dependencies, list)
            else []
        )
        valid: list[str] = []
        unknown: list[str] = []
        seen: set[str] = set()
        for dependency in raw_dependencies:
            if not dependency or dependency == label or dependency in seen:
                continue
            seen.add(dependency)
            if dependency in worker_labels:
                valid.append(dependency)
            else:
                unknown.append(dependency)
        contract["depends_on"] = valid
        dependencies[label] = valid
        unknown_dependencies[label] = unknown
    return contract_by_label, dependencies, unknown_dependencies


def _multi_run_receipt_checks(workers, completed, failures, audits):
    checks = []
    for label, _agent in workers:
        passed = label in completed
        checks.append(
            {
                "name": f"lane {label} acceptance",
                "passed": passed,
                "evidence": "worker contract passed"
                if passed
                else failures.get(label, "worker contract failed or was skipped"),
            }
        )
    checks.extend(
        {
            "name": name,
            "passed": not findings,
            "evidence": "; ".join(findings[:6]) if findings else "passed",
        }
        for name, applicable, findings in audits
        if applicable
    )
    return checks


class CommandsAgentExecutionMixin:
    async def _await_multi_preflight_thread(
        self, run_id: str, workspace: Path, function, *args, **kwargs
    ):
        return await self._await_task_workspace_preflight(
            workspace,
            function,
            *args,
            run_id=run_id,
            job_store=self.jobs,
            **kwargs,
        )

    async def _evaluate_multi_worker_acceptance_off_thread(
        self,
        workspace: Path,
        label: str,
        worker_contract: dict[str, object],
        job_run_id: str | None = None,
    ) -> tuple[bool, list[str], dict[str, Any]]:
        cancel_event = threading.Event()
        try:
            return await asyncio.to_thread(
                self._evaluate_multi_worker_acceptance,
                workspace,
                label,
                worker_contract,
                cancel_event=cancel_event,
                job_run_id=job_run_id,
            )
        except asyncio.CancelledError:
            cancel_event.set()
            raise

    async def _execute_pending_multi_plan(self, update: Update, session_id: str):
        if getattr(self, "_shutting_down", False):
            return
        locks = getattr(self, "_session_run_locks", None)
        if locks is None:
            locks = self._session_run_locks = {}
        lock = locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            await self._reply_logged(
                update,
                "⏳ Another agent run is active in this chat. Wait for it or cancel it before approving this plan.",
            )
            return
        try:
            async with lock:
                current = asyncio.current_task()
                if current:
                    self._active_run_tasks_by_session[session_id] = current
                try:
                    return await self._execute_pending_multi_plan_impl(update, session_id)
                finally:
                    if self._active_run_tasks_by_session.get(session_id) is current:
                        self._active_run_tasks_by_session.pop(session_id, None)
                    run_id = self._active_run_ids_by_session.get(session_id)
                    if run_id:
                        await self._cleanup_abandoned_multi_run(
                            update, session_id, run_id
                        )
        finally:
            if locks.get(session_id) is lock and not lock.locked():
                locks.pop(session_id, None)

    async def _cleanup_abandoned_multi_run(
        self, update: Update, session_id: str, run_id: str
    ) -> None:
        worker_tasks = list(self._active_worker_tasks_by_run.pop(run_id, {}))
        heartbeat = self._active_run_heartbeats_by_run.pop(run_id, None)
        for task in worker_tasks:
            self._cancel_task_once(task)
        if heartbeat:
            self._cancel_task_once(heartbeat)
        tasks = worker_tasks + ([heartbeat] if heartbeat else [])
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        failure = "Multi-agent orchestration exited unexpectedly."
        try:
            job = await asyncio.to_thread(self.jobs.get_job, run_id)
            if job["status"] in {"running", "cancel_requested"}:
                for lane in job["lanes"]:
                    if lane["status"] in {"queued", "running"}:
                        try:
                            await asyncio.to_thread(
                                self.jobs.update_lane,
                                run_id,
                                str(lane["label"]),
                                "failed",
                                error=failure,
                            )
                        except JobStateError:
                            pass
                await asyncio.to_thread(
                    self.jobs.finish, run_id, succeeded=False, error=failure
                )
                await self._reply_logged(
                    update,
                    "⚠️ Multi-agent run stopped unexpectedly and was marked failed. "
                    "Its task workspace remains available for inspection.",
                )
        except JobStateError:
            pass
        except Exception:
            log.exception("Failed to finalize abandoned multi-agent run %s", run_id)
        finally:
            self._release_multi_run_tracking(session_id, run_id)

    async def _execute_pending_multi_plan_impl(self, update: Update, session_id: str):
        pending = self._get_pending_multi_plan(session_id)
        if not pending:
            await self._reply_logged(
                update,
                "No pending multi-agent plan.\nStart one with <code>/agent multi &lt;goal&gt;</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        if not self._is_pending_requester(update, pending):
            await self._reply_logged(
                update, "Only the requester can approve this plan."
            )
            return
        review = pending.get("review") if isinstance(pending.get("review"), dict) else {}
        if not await self._require_complete_plan_review(update, pending):
            return
        if review.get("approval_blocked"):
            await self._reply_logged(
                update,
                "Approval blocked; edit the plan to expose all commands.",
            )
            return
        if review.get("second_confirmation_required") and not review.get("second_confirmed"):
            await self._prompt_second_confirmation(update, pending)
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
        run_id = f"multi-{time.time_ns()}-{session_id[-6:]}"
        try:
            await self._execute_multi_agent_plan(
                update=update,
                session_id=session_id,
                goal=goal,
                workers=resolved_workers,
                plan_payload=plan_payload,
                run_id=run_id,
            )
        except asyncio.CancelledError:
            worker_tasks = list(self._active_worker_tasks_by_run.pop(run_id, {}))
            for task in worker_tasks:
                self._cancel_task_once(task)
            await asyncio.gather(*worker_tasks, return_exceptions=True)
            heartbeat = self._active_run_heartbeats_by_run.pop(run_id, None)
            if heartbeat:
                self._cancel_task_once(heartbeat)
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
            await self._cancel_durable_delegation(
                self.jobs, session_id, run_id, owns_workspace=False
            )
            if self._active_run_ids_by_session.get(session_id) == run_id:
                self._active_run_ids_by_session.pop(session_id, None)
            raise
        except Exception:
            if getattr(self, "_active_run_ids_by_session", {}).get(session_id) == run_id:
                try:
                    await asyncio.to_thread(self.jobs.get_job, run_id)
                except JobStateError:
                    pass
                else:
                    raise
            log.exception("Multi-agent run failed before worker execution")
            await self._reply_logged(
                update,
                "⚠️ Could not prepare the approved run. No agent was started. "
                "Create and approve a new plan to retry.",
            )

    def _release_multi_run_tracking(self, session_id: str, run_id: str) -> None:
        self._active_worker_tasks_by_run.pop(run_id, None)
        if self._active_run_ids_by_session.get(session_id) == run_id:
            self._active_run_ids_by_session.pop(session_id, None)

    async def _begin_multi_run_progress(self, update, session_id: str, run_id: str):
        self._active_run_ids_by_session[session_id] = run_id
        requesters = getattr(self, "_active_run_requesters_by_session", None)
        if requesters is None:
            requesters = self._active_run_requesters_by_session = {}
        requesters[session_id] = getattr(
            getattr(update, "effective_user", None), "id", None
        )
        return await self._reply_logged(
            update,
            "⏳ Approved multi-agent run in progress. Preparation may take a moment.",
            reply_markup=self._inline_cancel_keyboard(run_id),
        )

    async def _execute_multi_agent_plan(
        self,
        update: Update,
        session_id: str,
        goal: str,
        workers: list[tuple[str, str]],
        plan_payload: dict[str, object],
        run_id: str,
    ):
        async with self._memory_request_guard(session_id) as clear_event:
            return await self._execute_multi_agent_plan_impl(
                update, session_id, goal, workers, plan_payload, run_id, clear_event
            )

    async def _execute_multi_agent_plan_impl(
        self,
        update: Update,
        session_id: str,
        goal: str,
        workers: list[tuple[str, str]],
        plan_payload: dict[str, object],
        run_id: str,
        clear_event: asyncio.Event,
    ):
        run_started_clock = time.monotonic()
        run_started_at = self._utc_now()
        await self._begin_multi_run_progress(update, session_id, run_id)
        multi_workspace = await self._create_task_workspace_safely(goal)
        multi_workspace_label = self._workspace_rel_label(multi_workspace)
        agents_path = await self._await_multi_preflight_thread(
            run_id,
            multi_workspace,
            self._write_agents_plan_file, multi_workspace, plan_payload
        )

        await self._await_multi_preflight_thread(
            run_id, multi_workspace,
            lambda: os.close(open_directory_at(multi_workspace, ("handoff",), create=True, private=True)),
        )
        try:
            checkpoint = await self._await_multi_preflight_thread(
                run_id,
                multi_workspace,
                initialize_artifact_repository,
                multi_workspace,
                run_id,
            )
        except ArtifactError as exc:
            log.warning("Git checkpoint failed for multi-agent run %s: %s", run_id, exc)
            await self._reply_logged(
                update,
                "🛑 Git checkpoint failed; no workers started. Workspace preserved. "
                "See the local LightClaw log.",
            )
            return
        before_multi = await self._await_multi_preflight_thread(
            run_id,
            multi_workspace,
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
                reply_markup=self._inline_cancel_keyboard(run_id),
            )
            worker_msgs.append(worker_msg)

        (
            worker_contract_by_label,
            dependency_map,
            unknown_dependency_map,
        ) = _multi_worker_dependencies(workers, plan_payload)
        workers_by_label = dict(workers)

        repair_attempts = max(
            0,
            min(2, int(getattr(self.config, "local_agent_multi_repair_attempts", 1))),
        )
        durable_plan: list[dict[str, object]] = []
        for label, agent in workers:
            contract = worker_contract_by_label.get(label, {})
            owned_paths = contract.get("owned_paths")
            durable_plan.append(
                {
                    "label": label,
                    "worker": agent,
                    "depends_on": dependency_map.get(label, []),
                    "owned_paths": owned_paths if isinstance(owned_paths, list) else [],
                    "idempotent": False,
                    "resumable": False,
                    "max_attempts": repair_attempts + 1,
                }
            )
        try:
            await await_thread_completion(
                self.jobs.create_job,
                workspace=multi_workspace,
                session_id=session_id,
                requester_user_id=getattr(
                    getattr(update, "effective_user", None), "id", None
                ),
                goal=goal,
                approved_scope=f"LightClaw-owned task workspace: {multi_workspace_label}",
                risk_level="medium",
                capability_profile=self.config.local_agent_capability_profile,
                plan=durable_plan,
                priority=0,
                max_retries=repair_attempts,
                resumable=False,
                status="queued",
                run_id=run_id,
            )
            claimed_job = await await_thread_completion(
                self.jobs.claim_next,
                workspace=multi_workspace,
                worker_pid=None,
                run_id=run_id,
            )
        except (JobConflictError, JobStateError) as exc:
            await self._reply_logged(
                update,
                f"🛑 Durable job control refused this plan: {_escape_html(str(exc))}\n"
                f"Owned workspace preserved for review: <code>{_escape_html(multi_workspace_label)}</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        if not claimed_job or claimed_job["run_id"] != run_id:
            await self._reply_logged(
                update,
                f"⏳ Run <code>{_escape_html(run_id)}</code> was not started; it remains queued and will not auto-start. "
                "<code>/agent runs</code> in this chat offers one-tap cancellation to the requester; "
                "otherwise use <code>lightclaw jobs cancel &lt;run-id&gt;</code> on the host, "
                "then resubmit after the workspace is clear.",
                parse_mode=ParseMode.HTML,
            )
            return
        multi_execution_task = asyncio.current_task()

        async def _multi_job_heartbeat() -> None:
            while True:
                await asyncio.sleep(30)
                current_job, stop = await self._durable_job_heartbeat(self.jobs, run_id)
                if stop:
                    return
                if not current_job or current_job["status"] != "cancel_requested":
                    continue
                for task in list(running):
                    self._cancel_task_once(task)
                await self._cancel_durable_delegation(
                    self.jobs,
                    session_id,
                    run_id,
                    multi_workspace,
                    owns_workspace=False,
                )
                if multi_execution_task:
                    self._cancel_task_once(multi_execution_task)
                return

        durable_heartbeat = asyncio.create_task(_multi_job_heartbeat())
        self._active_run_heartbeats_by_run[run_id] = durable_heartbeat

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
                    await progress_msg.edit_text(
                        f"{tag}\n{text}",
                        reply_markup=self._inline_cancel_keyboard(run_id),
                    )
                except Exception:
                    pass

            last_result = ""
            last_failures: list[str] = []
            attempt_evidence: list[dict[str, object]] = []
            for attempt in range(repair_attempts + 1):
                if attempt > 0:
                    await asyncio.to_thread(self.jobs.update_lane, run_id, label, "queued")
                await asyncio.to_thread(
                    self.jobs.update_lane,
                    run_id,
                    label,
                    "running",
                    increment_attempt=True,
                )
                task_prompt = worker_task
                if attempt > 0:
                    task_prompt = self._build_multi_agent_repair_task(
                        label=label,
                        goal=goal,
                        workers=workers,
                        worker_plan=worker_contract,
                        acceptance_failures=last_failures,
                        previous_result=last_result,
                        task_workspace_label=multi_workspace_label,
                    )

                try:
                    worker_evidence: dict[str, object] = {}
                    result = await self._run_local_agent_task(
                        session_id=session_id,
                        agent=agent,
                        task=task_prompt,
                        progress_cb=_worker_progress_update,
                        include_workspace_delta=False,
                        workspace_dir=multi_workspace,
                        emit_receipt=False,
                        evidence_sink=worker_evidence,
                        manage_job=False,
                        initialize_artifact=False,
                        process_owner_run_id=run_id,
                    )
                    attempt_evidence.append(worker_evidence)
                except Exception as e:
                    result = f"⚠️ Worker failed: {e}"
                    attempt_evidence.append(
                        {
                            "commands": [],
                            "checks": [
                                {
                                    "name": f"{label} execution",
                                    "passed": False,
                                    "evidence": "worker raised an exception",
                                }
                            ],
                            "failures": [str(e)],
                        }
                    )

                runtime_ok = self._delegation_result_state(result) == "success"
                handoff_data: dict[str, Any] = {}
                if runtime_ok:
                    acceptance_ok, acceptance_failures, handoff_data = await self._evaluate_multi_worker_acceptance_off_thread(
                        multi_workspace,
                        label,
                        worker_contract,
                        job_run_id=run_id,
                    )
                else:
                    acceptance_ok = False
                    acceptance_failures = ["execution did not finish cleanly"]

                enriched_result = self._append_multi_acceptance_report(
                    result,
                    acceptance_failures,
                    handoff_data,
                )
                if runtime_ok and acceptance_ok:
                    await asyncio.to_thread(
                        self.jobs.update_lane,
                        run_id,
                        label,
                        "succeeded",
                    )
                    try:
                        await progress_msg.edit_text(f"{tag}\n✅ Worker completed.")
                    except Exception:
                        pass
                    return (label, agent, enriched_result, True, attempt_evidence)

                last_result = enriched_result
                last_failures = acceptance_failures or ["worker execution failed"]
                if attempt >= repair_attempts:
                    lane_failure_reasons[label] = "; ".join(last_failures)[:500]
                    await asyncio.to_thread(
                        self.jobs.update_lane,
                        run_id,
                        label,
                        "failed",
                        error="; ".join(last_failures)[:500],
                    )
                    try:
                        await progress_msg.edit_text(f"{tag}\n⚠️ Worker finished with issues.")
                    except Exception:
                        pass
                    return (label, agent, last_result, False, attempt_evidence)

                reason = self._short_progress_text("; ".join(last_failures), max_chars=180)
                try:
                    await progress_msg.edit_text(
                        f"{tag}\n🔧 Repair attempt {attempt + 1}/{repair_attempts}: {reason}",
                        reply_markup=self._inline_cancel_keyboard(run_id),
                    )
                except Exception:
                    pass

            return (label, agent, last_result or "⚠️ Worker failed.", False, attempt_evidence)

        remaining = set(workers_by_label.keys())
        completed_ok: set[str] = set()
        failed: set[str] = set()
        results_by_label: dict[str, object] = {}
        lane_failure_reasons: dict[str, str] = {}
        evidence_by_label: dict[str, list[dict[str, object]]] = {}
        index_by_label = {label: idx for idx, (label, _) in enumerate(workers)}
        wait_status_by_label: dict[str, str] = {}
        running: dict[asyncio.Task[Any], str] = {}
        self._active_worker_tasks_by_run[run_id] = running

        async def _set_worker_status(label: str, status_text: str):
            if wait_status_by_label.get(label) == status_text:
                return
            wait_status_by_label[label] = status_text
            index = index_by_label[label]
            tag = self._multi_agent_tag(label, workers_by_label[label], index)
            try:
                await worker_msgs[index].edit_text(
                    f"{tag}\n{status_text}",
                    reply_markup=self._inline_cancel_keyboard(run_id),
                )
            except Exception:
                pass

        for label in list(remaining):
            unknown_deps = unknown_dependency_map.get(label) or []
            if not unknown_deps:
                continue
            remaining.discard(label)
            failed.add(label)
            reason = ", ".join(unknown_deps)
            lane_failure_reasons[label] = f"unknown dependency: {reason}"
            skip_text = f"⚠️ Skipped because AGENTS.md references unknown dependency: {reason}"
            results_by_label[label] = skip_text
            await asyncio.to_thread(
                self.jobs.update_lane,
                run_id,
                label,
                "skipped",
                error=reason[:500],
            )
            await _set_worker_status(label, skip_text)

        while remaining or running:
            running_labels = set(running.values())
            blocked_now: list[str] = []
            for label in list(remaining):
                if label in running_labels:
                    continue
                dep_list = dependency_map.get(label) or []
                if any(dep in failed for dep in dep_list):
                    blocked_now.append(label)

            for label in blocked_now:
                remaining.discard(label)
                failed.add(label)
                dep_list = dependency_map.get(label) or []
                reason = ", ".join(d for d in dep_list if d in failed) or "failed dependency"
                lane_failure_reasons[label] = f"failed dependency: {reason}"
                skip_text = f"⚠️ Skipped because dependency failed: {reason}"
                results_by_label[label] = skip_text
                await asyncio.to_thread(
                    self.jobs.update_lane,
                    run_id,
                    label,
                    "skipped",
                    error=reason[:500],
                )
                await _set_worker_status(label, skip_text)

            ready: list[str] = []
            for label in list(remaining):
                if label in running_labels:
                    continue
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

            for label in ready:
                task = asyncio.create_task(
                    _run_worker(
                        index_by_label[label],
                        label,
                        workers_by_label[label],
                        worker_msgs[index_by_label[label]],
                    )
                )
                running[task] = label

            if not running:
                for label in list(remaining):
                    remaining.discard(label)
                    failed.add(label)
                    lane_failure_reasons[label] = "unresolved dependency cycle in AGENTS.md"
                    skip_text = "⚠️ Skipped due to unresolved dependency cycle in AGENTS.md."
                    results_by_label[label] = skip_text
                    await asyncio.to_thread(
                        self.jobs.update_lane,
                        run_id,
                        label,
                        "skipped",
                        error="unresolved dependency cycle",
                    )
                    await _set_worker_status(label, skip_text)
                break

            done, _ = await asyncio.wait(
                set(running.keys()),
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in done:
                label = running.pop(task, "")
                if label:
                    remaining.discard(label)
                result = task.result()
                label, _agent, worker_result, ok, attempt_evidence = result
                results_by_label[label] = worker_result
                evidence_by_label[label] = attempt_evidence
                if ok:
                    completed_ok.add(label)
                else:
                    failed.add(label)

        self._release_multi_run_tracking(session_id, run_id)

        after_multi = await asyncio.to_thread(
            self._snapshot_workspace_state, multi_workspace
        )
        multi_delta = self._summarize_workspace_delta(before_multi, after_multi)
        api_audit_applicable, api_audit_findings = await asyncio.to_thread(
            self._audit_multi_lane_api_contracts,
            multi_workspace,
            worker_contract_by_label,
        )
        findings_audit_applicable, findings_audit_findings = await asyncio.to_thread(
            self._audit_multi_lane_findings_flow,
            multi_workspace,
            worker_contract_by_label,
        )
        deliverables_audit_applicable, deliverables_audit_findings = await asyncio.to_thread(
            self._audit_multi_lane_deliverables,
            multi_workspace,
            worker_contract_by_label,
        )

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

        if api_audit_applicable:
            final_lines.append(
                "Cross-lane API audit: passed"
                if not api_audit_findings
                else "Cross-lane API audit: failed"
            )
            for finding in api_audit_findings[:6]:
                final_lines.append(f"- {finding}")
            final_lines.append("")

        if findings_audit_applicable:
            final_lines.append(
                "Cross-lane findings audit: passed"
                if not findings_audit_findings
                else "Cross-lane findings audit: failed"
            )
            for finding in findings_audit_findings[:6]:
                final_lines.append(f"- {finding}")
            final_lines.append("")

        if deliverables_audit_applicable:
            final_lines.append(
                "Deliverables audit: passed"
                if not deliverables_audit_findings
                else "Deliverables audit: failed"
            )
            for finding in deliverables_audit_findings[:6]:
                final_lines.append(f"- {finding}")
            final_lines.append("")

        final_lines.append(multi_delta)
        if failed:
            final_lines.extend(
                [
                    "",
                    "Failed lanes cannot be replayed safely; create and review a new plan to retry.",
                ]
            )

        file_changes = await asyncio.to_thread(
            self._workspace_file_changes,
            multi_workspace,
            before_multi,
            after_multi,
        )
        receipt_checks = _multi_run_receipt_checks(
            workers,
            completed_ok,
            lane_failure_reasons,
            (
                ("cross-lane API audit", api_audit_applicable, api_audit_findings),
                ("cross-lane findings audit", findings_audit_applicable, findings_audit_findings),
                ("deliverables audit", deliverables_audit_applicable, deliverables_audit_findings),
            ),
        )

        commands: list[dict[str, object]] = []
        failures: list[str] = []
        retries = 0
        for label, attempts in evidence_by_label.items():
            retries += max(0, len(attempts) - 1)
            for attempt in attempts:
                attempt_commands = attempt.get("commands")
                if isinstance(attempt_commands, list):
                    for command in attempt_commands:
                        if isinstance(command, dict):
                            commands.append({"lane": label, **command})
                attempt_failures = attempt.get("failures")
                if isinstance(attempt_failures, list):
                    failures.extend(f"{label}: {item}" for item in attempt_failures if str(item))
        failures.extend(
            f"{label}: {lane_failure_reasons.get(label, 'lane did not complete')}"
            for label in sorted(failed)
        )
        failures.extend(api_audit_findings)
        failures.extend(findings_audit_findings)
        failures.extend(deliverables_audit_findings)
        failures = list(dict.fromkeys(failures))

        handoffs: list[dict[str, object]] = []
        for label, _agent in workers:
            handoff_data, handoff_error = await asyncio.to_thread(
                self._load_multi_worker_handoff,
                multi_workspace,
                label,
            )
            if handoff_error:
                continue
            handoff_to = handoff_data.get("handoff")
            handoffs.append(
                {
                    "from": label,
                    "to": handoff_to if handoff_to else "final audit",
                    "status": handoff_data.get("status", "recorded"),
                    "path": self._multi_handoff_json_path(label),
                }
            )

        receipt_plan: list[dict[str, object]] = []
        for label, agent in workers:
            contract = worker_contract_by_label.get(label, {})
            receipt_plan.append(
                {
                    "label": label,
                    "worker": agent,
                    "model": "local CLI account routing",
                    "depends_on": list(dependency_map.get(label, [])),
                    "task": str(contract.get("role") or "implementation"),
                    "owned_paths": contract.get("owned_paths", []),
                }
            )
        receipt_output = self._receipt_output_dir(run_id)
        try:
            artifact_bundle = await asyncio.to_thread(
                create_patch_bundle,
                multi_workspace,
                receipt_output,
                run_id=run_id,
            )
        except ArtifactError as exc:
            artifact_bundle = {"error": str(exc), "diff_stat": "patch generation failed"}
            failures.append(f"patch generation failed: {exc}")
        artifact_paths = [
            item["path"] for item in file_changes if item.get("change") != "deleted"
        ]
        for key in ("patch", "manifest"):
            value = artifact_bundle.get(key)
            if value:
                artifact_paths.append(str(value))

        await self._record_multi_job_finalization(
            run_id, receipt_checks, failures, final_lines
        )

        receipt = {
            "run_id": run_id,
            "original_goal": goal,
            "approved_scope": f"LightClaw-owned task workspace: {multi_workspace_label}",
            "risk_level": "medium",
            "capability_profile": self.config.local_agent_capability_profile,
            "plan": receipt_plan,
            "started_at": run_started_at,
            "finished_at": self._utc_now(),
            "duration_seconds": round(time.monotonic() - run_started_clock, 3),
            "usage": {
                "provider": "local coding-agent CLIs",
                "tokens": None,
                "estimated_cost_usd": None,
                "note": "workers did not expose bounded usage to LightClaw",
            },
            "commands": commands,
            "file_changes": file_changes,
            "diff_summary": str(artifact_bundle.get("diff_stat") or "").strip()
            or self._compact_diff_summary(file_changes),
            "checks": receipt_checks,
            "handoffs": handoffs,
            "artifacts": artifact_paths,
            "failures": failures,
            "retries": retries,
            "disposition": "ready_for_review" if not failures else "failed",
            "checkpoint": checkpoint,
            "undo": f"lightclaw undo {multi_workspace.name} --apply",
        }
        receipt_json, receipt_markdown, _ = await await_thread_completion(
            write_receipt, receipt, receipt_output
        )
        final_lines.append("")
        final_lines.append(f"🧾 Receipt: `{self._workspace_rel_label(receipt_markdown)}`")
        final_lines.append(f"JSON: `{self._workspace_rel_label(receipt_json)}`")
        self._last_run_ids_by_session[session_id] = run_id
        self._last_run_receipts_by_session[session_id] = receipt_json.as_posix()
        requesters = getattr(self, "_last_run_requesters_by_session", None)
        if requesters is None:
            requesters = self._last_run_requesters_by_session = {}
        requesters[session_id] = getattr(
            getattr(update, "effective_user", None), "id", None
        )

        self._cancel_task_once(durable_heartbeat)
        try:
            await durable_heartbeat
        except asyncio.CancelledError:
            pass
        self._active_run_heartbeats_by_run.pop(run_id, None)
        if self._active_run_ids_by_session.get(session_id) == run_id:
            self._active_run_ids_by_session.pop(session_id, None)

        request_entry = (
            "[delegation-request]\n"
            "mode: multi\n"
            f"goal: {goal}\n"
            f"workers: {', '.join(f'{label}={agent}' for label, agent in workers)}"
        )
        memory_entry = self._build_multi_delegation_memory_entry(
            goal=goal,
            workspace_label=multi_workspace_label,
            workers=workers,
            results_by_label=results_by_label,
        )
        if not clear_event.is_set():
            await self._ingest_memory("user", request_entry, session_id, clear_event=clear_event)
            await self._ingest_memory(
                "assistant", memory_entry, session_id, clear_event=clear_event
            )
            if not self._llm_backoff_active():
                self._create_background_task(self.maybe_summarize(session_id))

        await self._send_response(None, update, "\n".join(final_lines).strip())
        await self._reply_logged(
            update,
            "Review the evidence before accepting the result.",
            reply_markup=self._inline_result_keyboard(run_id),
        )
