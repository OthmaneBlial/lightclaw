"""/agent command handlers and multi-agent execution orchestration."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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

    @staticmethod
    def _multi_handoff_lookup(data: dict[str, Any], dotted_path: str) -> Any:
        current: Any = data
        for part in dotted_path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    @staticmethod
    def _normalize_multi_api_method(raw: object) -> str:
        return re.sub(r"[^A-Z]", "", str(raw or "").strip().upper())

    @staticmethod
    def _normalize_multi_api_path(raw: object) -> str:
        value = str(raw or "").strip()
        if not value:
            return ""
        if "://" in value:
            try:
                parsed = urlsplit(value)
                value = parsed.path or "/"
            except Exception:
                pass
        value = value.split("?", 1)[0].split("#", 1)[0].strip()
        if not value:
            return ""
        if not value.startswith("/"):
            value = "/" + value.lstrip("/")
        value = re.sub(r"/{2,}", "/", value)
        value = re.sub(r"\{[^}/]+\}", "{}", value)
        value = re.sub(r":[A-Za-z0-9_]+", "{}", value)
        if len(value) > 1:
            value = value.rstrip("/")
        return value or "/"

    def _parse_multi_api_entries(self, items_obj: object) -> list[tuple[str, str]]:
        items = items_obj if isinstance(items_obj, list) else []
        parsed: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        for item in items[:64]:
            method = ""
            path = ""
            if isinstance(item, dict):
                method = self._normalize_multi_api_method(
                    item.get("method") or item.get("verb")
                )
                path = self._normalize_multi_api_path(
                    item.get("path") or item.get("route") or item.get("endpoint") or item.get("url")
                )
            elif isinstance(item, str):
                match = re.match(r"^\s*([A-Za-z]+)\s+(\S+)", item)
                if not match:
                    continue
                method = self._normalize_multi_api_method(match.group(1))
                path = self._normalize_multi_api_path(match.group(2))

            if not method or not path:
                continue
            key = (method, path)
            if key in seen:
                continue
            seen.add(key)
            parsed.append(key)

        return parsed

    def _extract_multi_api_entries(
        self,
        handoff_data: dict[str, Any],
        dotted_paths: tuple[str, ...],
    ) -> list[tuple[str, str]]:
        for dotted_path in dotted_paths:
            value = self._multi_handoff_lookup(handoff_data, dotted_path)
            parsed = self._parse_multi_api_entries(value)
            if parsed:
                return parsed
        return []

    def _extract_multi_string_list(
        self,
        handoff_data: dict[str, Any],
        dotted_paths: tuple[str, ...],
    ) -> list[str]:
        for dotted_path in dotted_paths:
            value = self._multi_handoff_lookup(handoff_data, dotted_path)
            items = value if isinstance(value, list) else []
            out: list[str] = []
            seen: set[str] = set()
            for item in items[:64]:
                text = ""
                if isinstance(item, dict):
                    text = str(
                        item.get("path")
                        or item.get("file")
                        or item.get("name")
                        or item.get("value")
                        or ""
                    ).strip()
                else:
                    text = str(item or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                out.append(text)
            if out:
                return out
        return []

    @staticmethod
    def _format_multi_api_entry(method: str, path: str) -> str:
        return f"{method} {path}"

    def _audit_multi_lane_api_contracts(
        self,
        workspace: Path,
        worker_contract_by_label: dict[str, dict[str, object]],
    ) -> tuple[bool, list[str]]:
        providers: list[str] = []
        consumers: list[str] = []
        for label, contract in worker_contract_by_label.items():
            role = str(contract.get("role") or "")
            if self._multi_is_backend_lane(label, role):
                providers.append(label)
            if self._multi_is_frontend_lane(label, role):
                consumers.append(label)

        if not providers or not consumers:
            return False, []

        findings: list[str] = []
        provider_endpoints: dict[str, list[tuple[str, str]]] = {}
        available_endpoints: set[tuple[str, str]] = set()

        for label in providers:
            handoff_data, error = self._load_multi_worker_handoff(workspace, label)
            if error:
                findings.append(f"`{label}` handoff unavailable for API audit: {error}")
                continue
            endpoints = self._extract_multi_api_entries(
                handoff_data,
                (
                    "outputs.endpoints",
                    "outputs.api_endpoints",
                    "handoff.endpoints",
                    "endpoints",
                ),
            )
            if not endpoints:
                findings.append(f"`{label}` handoff is missing `outputs.endpoints` for API audit")
                continue
            provider_endpoints[label] = endpoints
            available_endpoints.update(endpoints)

        for label in consumers:
            handoff_data, error = self._load_multi_worker_handoff(workspace, label)
            if error:
                findings.append(f"`{label}` handoff unavailable for API audit: {error}")
                continue
            api_calls = self._extract_multi_api_entries(
                handoff_data,
                (
                    "outputs.api_calls",
                    "outputs.http_calls",
                    "handoff.api_calls",
                    "api_calls",
                ),
            )
            if not api_calls:
                findings.append(f"`{label}` handoff is missing `outputs.api_calls` for API audit")
                continue
            if not available_endpoints:
                continue
            missing = [item for item in api_calls if item not in available_endpoints]
            if missing:
                preview = ", ".join(
                    f"`{self._format_multi_api_entry(method, path)}`"
                    for method, path in missing[:4]
                )
                findings.append(
                    f"`{label}` calls methods/routes not provided by backend lanes: {preview}"
                )

        deduped: list[str] = []
        seen: set[str] = set()
        for item in findings:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return True, deduped

    def _audit_multi_lane_findings_flow(
        self,
        workspace: Path,
        worker_contract_by_label: dict[str, dict[str, object]],
    ) -> tuple[bool, list[str]]:
        research_labels: list[str] = []
        review_labels: list[str] = []
        for label, contract in worker_contract_by_label.items():
            role = str(contract.get("role") or "")
            if self._multi_is_research_lane(label, role):
                research_labels.append(label)
            if self._multi_is_review_lane(label, role):
                review_labels.append(label)

        if not research_labels or not review_labels:
            return False, []

        findings: list[str] = []
        for label in research_labels:
            handoff_data, error = self._load_multi_worker_handoff(workspace, label)
            if error:
                findings.append(f"`{label}` handoff unavailable for findings audit: {error}")
                continue
            lane_findings = self._extract_multi_string_list(
                handoff_data,
                ("outputs.findings", "handoff.findings", "findings"),
            )
            if not lane_findings:
                findings.append(f"`{label}` handoff is missing `outputs.findings` for findings audit")

        research_set = set(research_labels)
        for label in review_labels:
            contract = worker_contract_by_label.get(label, {})
            deps_obj = contract.get("depends_on")
            deps = [str(item).strip() for item in deps_obj] if isinstance(deps_obj, list) else []
            if not any(dep in research_set for dep in deps):
                findings.append(
                    f"`{label}` should depend on at least one research/analysis lane for findings audit"
                )

        deduped: list[str] = []
        seen: set[str] = set()
        for item in findings:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return True, deduped

    def _audit_multi_lane_deliverables(
        self,
        workspace: Path,
        worker_contract_by_label: dict[str, dict[str, object]],
    ) -> tuple[bool, list[str]]:
        deliverable_labels: list[str] = []
        for label, contract in worker_contract_by_label.items():
            role = str(contract.get("role") or "")
            if self._multi_is_deliverable_lane(label, role):
                deliverable_labels.append(label)

        if not deliverable_labels:
            return False, []

        findings: list[str] = []
        for label in deliverable_labels:
            handoff_data, error = self._load_multi_worker_handoff(workspace, label)
            if error:
                findings.append(f"`{label}` handoff unavailable for deliverables audit: {error}")
                continue
            deliverables = self._extract_multi_string_list(
                handoff_data,
                ("outputs.deliverables", "handoff.deliverables", "deliverables"),
            )
            if not deliverables:
                findings.append(f"`{label}` handoff is missing `outputs.deliverables` for deliverables audit")
                continue

            reported_files = set(self._reported_multi_handoff_files(handoff_data))
            for item in deliverables[:12]:
                normalized = self._normalize_multi_contract_path(item)
                if not normalized:
                    continue
                looks_like_path = (
                    normalized in reported_files
                    or "/" in item
                    or "." in Path(normalized).name
                    or normalized.lower().startswith("readme")
                )
                if looks_like_path and not (workspace / normalized).exists():
                    findings.append(f"`{label}` deliverable does not exist in workspace: `{normalized}`")

        deduped: list[str] = []
        seen: set[str] = set()
        for item in findings:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return True, deduped

    def _run_multi_acceptance_command(
        self,
        workspace: Path,
        check: dict[str, Any],
    ) -> str:
        command = str(check.get("command") or "").strip()
        if not command:
            return "command_succeeds check is missing `command`"

        cwd_rel = self._normalize_multi_contract_path(str(check.get("cwd") or ""))
        cwd = workspace / cwd_rel if cwd_rel else workspace
        if not cwd.exists():
            return f"command_succeeds cwd does not exist: `{cwd_rel}`"
        if not cwd.is_dir():
            return f"command_succeeds cwd is not a directory: `{cwd_rel}`"

        try:
            argv = shlex.split(command)
        except Exception as e:
            return f"invalid command_succeeds command `{command}`: {e}"
        if not argv:
            return f"invalid command_succeeds command `{command}`"

        try:
            timeout_sec = int(check.get("timeout_sec") or 20)
        except Exception:
            timeout_sec = 20
        timeout_sec = max(1, min(45, timeout_sec))

        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return f"command timed out after {timeout_sec}s: `{command}`"
        except Exception as e:
            return f"command failed to start `{command}`: {e}"

        if completed.returncode == 0:
            return ""

        output = "\n".join(
            part.strip()
            for part in [completed.stdout or "", completed.stderr or ""]
            if part and part.strip()
        )
        output_preview = self._short_progress_text(output, max_chars=220) if output else ""
        location = f" in `{cwd_rel}`" if cwd_rel else ""
        detail = f": {output_preview}" if output_preview else ""
        return (
            f"command failed{location} (exit {completed.returncode}): `{command}`{detail}"
        )

    def _multi_value_is_nonempty(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, set)):
            return any(self._multi_value_is_nonempty(item) for item in value)
        if isinstance(value, dict):
            return bool(value)
        return True

    def _load_multi_worker_handoff(
        self,
        workspace: Path,
        label: str,
    ) -> tuple[dict[str, Any], str]:
        path = workspace / self._multi_handoff_json_path(label)
        if not path.exists():
            return {}, f"missing `{self._multi_handoff_json_path(label)}`"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            return {}, f"invalid JSON in `{self._multi_handoff_json_path(label)}`: {e}"
        if not isinstance(raw, dict):
            return {}, f"`{self._multi_handoff_json_path(label)}` must contain a JSON object"
        return raw, ""

    def _reported_multi_handoff_files(self, handoff_data: dict[str, Any]) -> list[str]:
        changed_files_obj = handoff_data.get("changed_files")
        changed_files = changed_files_obj if isinstance(changed_files_obj, list) else []
        reported: list[str] = []
        seen: set[str] = set()
        for item in changed_files[:64]:
            value = self._normalize_multi_contract_path(str(item or ""))
            if not value or value in seen:
                continue
            seen.add(value)
            reported.append(value)
        return reported

    def _evaluate_multi_worker_acceptance(
        self,
        workspace: Path,
        label: str,
        worker_contract: dict[str, object],
    ) -> tuple[bool, list[str], dict[str, Any]]:
        checks_obj = worker_contract.get("acceptance_checks")
        checks = (
            [item for item in checks_obj if isinstance(item, dict)]
            if isinstance(checks_obj, list)
            else []
        )
        if not checks:
            return True, [], {}

        owned_paths = self._normalize_multi_owned_paths(
            worker_contract.get("owned_paths"),
            label=label,
            role=str(worker_contract.get("role") or "implementation"),
        )
        failures: list[str] = []
        handoff_data: dict[str, Any] = {}
        handoff_error = ""
        handoff_loaded = False
        reported_files_cache: list[str] | None = None

        def load_handoff() -> tuple[dict[str, Any], str]:
            nonlocal handoff_data, handoff_error, handoff_loaded
            if handoff_loaded:
                return handoff_data, handoff_error
            handoff_loaded = True
            handoff_data, handoff_error = self._load_multi_worker_handoff(workspace, label)
            return handoff_data, handoff_error

        def reported_files() -> list[str]:
            nonlocal reported_files_cache
            if reported_files_cache is not None:
                return reported_files_cache
            data, _ = load_handoff()
            reported_files_cache = self._reported_multi_handoff_files(data)
            return reported_files_cache

        for check in checks:
            kind = str(check.get("type") or "").strip().lower()

            if kind == "file_exists":
                rel_path = self._normalize_multi_contract_path(str(check.get("path") or ""))
                if not rel_path or not (workspace / rel_path).is_file():
                    failures.append(f"missing required file `{rel_path or '(invalid path)'}`")
                continue

            if kind == "handoff_json":
                rel_path = (
                    self._normalize_multi_contract_path(str(check.get("path") or ""))
                    or self._multi_handoff_json_path(label)
                )
                target = workspace / rel_path
                if not target.is_file():
                    failures.append(f"missing handoff JSON `{rel_path}`")
                    continue
                try:
                    raw = json.loads(target.read_text(encoding="utf-8"))
                except Exception as e:
                    failures.append(f"invalid handoff JSON `{rel_path}`: {e}")
                    continue
                if not isinstance(raw, dict):
                    failures.append(f"`{rel_path}` must contain a JSON object")
                    continue
                lane_value = str(raw.get("lane") or "").strip().lower()
                if lane_value != label.lower():
                    failures.append(f"`{rel_path}` lane must be `{label}`")
                if not str(raw.get("summary") or "").strip():
                    failures.append(f"`{rel_path}` must include a non-empty summary")
                if not isinstance(raw.get("changed_files"), list):
                    failures.append(f"`{rel_path}` must include a changed_files list")
                continue

            if kind == "glob_nonempty":
                pattern = self._normalize_multi_contract_path(str(check.get("pattern") or ""))
                if not pattern:
                    failures.append("invalid glob_nonempty pattern")
                    continue
                try:
                    matches = [item for item in workspace.glob(pattern) if item.is_file()]
                except Exception as e:
                    failures.append(f"invalid glob pattern `{pattern}`: {e}")
                    continue
                if not matches:
                    failures.append(f"no files matched `{pattern}`")
                continue

            if kind == "command_succeeds":
                command_failure = self._run_multi_acceptance_command(workspace, check)
                if command_failure:
                    failures.append(command_failure)
                continue

            if kind == "json_field_nonempty":
                _, error = load_handoff()
                if error:
                    failures.append(error)
                    continue
                field = str(check.get("field") or "").strip()
                if not field:
                    failures.append("json_field_nonempty check is missing `field`")
                    continue
                value = self._multi_handoff_lookup(handoff_data, field)
                if not self._multi_value_is_nonempty(value):
                    failures.append(f"handoff JSON field `{field}` must be non-empty")
                continue

            if kind == "reported_files_exist":
                _, error = load_handoff()
                if error:
                    failures.append(error)
                    continue
                reported = reported_files()
                if not reported:
                    failures.append("handoff JSON must list at least one changed file")
                    continue
                missing = [path for path in reported if not (workspace / path).exists()]
                if missing:
                    failures.append(
                        "reported changed_files do not exist: "
                        + ", ".join(f"`{path}`" for path in missing[:4])
                    )
                continue

            if kind == "owned_path_touched":
                if not owned_paths:
                    failures.append("owned_path_touched requested but worker has no owned_paths")
                    continue
                reported = reported_files()
                if not reported:
                    failures.append("cannot verify owned_paths because handoff JSON has no changed_files")
                    continue
                if not any(self._multi_path_matches_any(path, owned_paths) for path in reported):
                    failures.append("no reported changed_files are inside owned_paths")
                continue

            if kind == "owned_paths_only":
                if not owned_paths:
                    failures.append("owned_paths_only requested but worker has no owned_paths")
                    continue
                reported = reported_files()
                if not reported:
                    failures.append("cannot verify owned_paths because handoff JSON has no changed_files")
                    continue
                out_of_bounds = [
                    path
                    for path in reported
                    if not path.startswith("handoff/")
                    and not self._multi_path_matches_any(path, owned_paths)
                ]
                if out_of_bounds:
                    failures.append(
                        "reported changed_files outside owned_paths: "
                        + ", ".join(f"`{path}`" for path in out_of_bounds[:4])
                    )

        deduped: list[str] = []
        seen: set[str] = set()
        for item in failures:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return not deduped, deduped, handoff_data

    def _append_multi_acceptance_report(
        self,
        result_text: str,
        failures: list[str],
        handoff_data: dict[str, Any] | None = None,
    ) -> str:
        lines = [(result_text or "").strip()]
        lines.append("")
        lines.append("Acceptance: passed" if not failures else "Acceptance: failed")
        for failure in failures[:6]:
            lines.append(f"- {failure}")

        handoff = handoff_data if isinstance(handoff_data, dict) else {}
        handoff_summary = self._short_progress_text(
            str(handoff.get("summary") or ""),
            max_chars=220,
        )
        if handoff_summary:
            lines.append(f"Handoff summary: {handoff_summary}")
        reported_files = self._reported_multi_handoff_files(handoff) if handoff else []
        if reported_files:
            preview = ", ".join(f"`{path}`" for path in reported_files[:6])
            if len(reported_files) > 6:
                preview += ", ..."
            lines.append(f"Reported files: {preview}")
        return "\n".join(line for line in lines if line).strip()


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
            return self._delegation_result_state(worker_result) == "success"

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

        repair_attempts = max(
            0,
            min(2, int(getattr(self.config, "local_agent_multi_repair_attempts", 1))),
        )

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

            last_result = ""
            last_failures: list[str] = []

            for attempt in range(repair_attempts + 1):
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
                    result = await self._run_local_agent_task(
                        session_id=session_id,
                        agent=agent,
                        task=task_prompt,
                        progress_cb=_worker_progress_update,
                        include_workspace_delta=False,
                        workspace_dir=multi_workspace,
                    )
                except Exception as e:
                    result = f"⚠️ Worker failed: {e}"

                runtime_ok = _is_success_result(result)
                handoff_data: dict[str, Any] = {}
                if runtime_ok:
                    acceptance_ok, acceptance_failures, handoff_data = await asyncio.to_thread(
                        self._evaluate_multi_worker_acceptance,
                        multi_workspace,
                        label,
                        worker_contract,
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
                    try:
                        await progress_msg.edit_text(f"{tag}\n✅ Worker completed.")
                    except Exception:
                        pass
                    return (label, agent, enriched_result, True)

                last_result = enriched_result
                last_failures = acceptance_failures or ["worker execution failed"]
                if attempt >= repair_attempts:
                    try:
                        await progress_msg.edit_text(f"{tag}\n⚠️ Worker finished with issues.")
                    except Exception:
                        pass
                    return (label, agent, last_result, False)

                reason = self._short_progress_text("; ".join(last_failures), max_chars=180)
                try:
                    await progress_msg.edit_text(
                        f"{tag}\n🔧 Repair attempt {attempt + 1}/{repair_attempts}: {reason}"
                    )
                except Exception:
                    pass

            return (label, agent, last_result or "⚠️ Worker failed.", False)

        remaining = set(workers_by_label.keys())
        completed_ok: set[str] = set()
        failed: set[str] = set()
        results_by_label: dict[str, object] = {}
        index_by_label = {label: idx for idx, (label, _) in enumerate(workers)}
        wait_status_by_label: dict[str, str] = {}
        running: dict[asyncio.Task[Any], str] = {}

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

        for label in list(remaining):
            unknown_deps = unknown_dependency_map.get(label) or []
            if not unknown_deps:
                continue
            remaining.discard(label)
            failed.add(label)
            reason = ", ".join(unknown_deps)
            skip_text = f"⚠️ Skipped because AGENTS.md references unknown dependency: {reason}"
            results_by_label[label] = skip_text
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
                skip_text = f"⚠️ Skipped because dependency failed: {reason}"
                results_by_label[label] = skip_text
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
                    skip_text = "⚠️ Skipped due to unresolved dependency cycle in AGENTS.md."
                    results_by_label[label] = skip_text
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
