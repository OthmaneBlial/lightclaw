"""Multi-agent planning, normalization, and AGENTS.md contract helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path


class DelegationMultiTaskMixin:
    @staticmethod
    def _classify_pending_multi_reply(text: str) -> str:
        normalized = re.sub(r"[^a-z]+", "", (text or "").strip().lower())
        if normalized in {"yes", "y", "confirm", "continue", "go"}:
            return "confirm"
        if normalized in {"no", "n", "cancel", "stop"}:
            return "cancel"
        return "other"

    def _render_pending_multi_reminder(self, session_id: str) -> str:
        remaining = self._pending_multi_plan_remaining_sec(session_id)
        mins = max(1, int((remaining + 59) // 60))
        return (
            "A multi-agent plan is pending confirmation.\n"
            "Use <code>/agent multi confirm</code> (or reply <code>yes</code>) to run it.\n"
            "Use <code>/agent multi edit &lt;feedback&gt;</code> to regenerate it.\n"
            f"Use <code>/agent multi cancel</code> (or reply <code>no</code>) to discard it.\n"
            f"Pending plan expires in about <code>{mins}m</code>."
        )

    def _build_multi_agent_worker_task(
        self,
        label: str,
        goal: str,
        workers: list[tuple[str, str]],
        worker_plan: dict[str, object] | None = None,
        task_workspace_label: str = "",
    ) -> str:
        roster = ", ".join(f"{name}={agent}" for name, agent in workers)
        role = str((worker_plan or {}).get("role") or "implementation").strip() or "implementation"
        lane_hint = "Focus only on your lane and avoid unrelated files."
        handoff_contract_hint = (
            "Keep handoff JSON accurate and machine-readable for downstream verification."
        )
        if self._multi_is_backend_lane(label, role):
            lane_hint = (
                "Focus on backend APIs, data models, persistence, and backend tests."
            )
            handoff_contract_hint = (
                "In handoff JSON outputs.endpoints, list each served HTTP method/path as strings like `GET /api/items`."
            )
        elif self._multi_is_frontend_lane(label, role):
            lane_hint = (
                "Focus on frontend UI, routing/state, and integration with backend API contracts."
            )
            handoff_contract_hint = (
                "In handoff JSON outputs.api_calls, list each backend HTTP method/path the frontend calls as strings like `GET /api/items`."
            )
        elif self._multi_is_research_lane(label, role):
            lane_hint = (
                "Focus on research, analysis, synthesis, and crisp evidence-backed findings."
            )
            handoff_contract_hint = (
                "In handoff JSON outputs.findings, list the key findings, caveats, or recommendations as a machine-readable list."
            )
        elif self._multi_is_review_lane(label, role):
            lane_hint = (
                "Focus on validation, critique, gap detection, and practical recommendations."
            )
            handoff_contract_hint = (
                "In handoff JSON outputs.findings, list the validated findings, risks, caveats, or unresolved issues as a machine-readable list."
            )
        elif self._multi_is_authoring_lane(label, role):
            lane_hint = (
                "Focus on producing the requested artifact clearly and keeping the deliverable set explicit."
            )
            handoff_contract_hint = (
                "In handoff JSON outputs.deliverables, list the produced artifacts as a machine-readable list of paths or artifact names."
            )
        elif self._multi_is_docs_lane(label, role):
            lane_hint = (
                "Focus on documentation: setup, architecture, usage, and developer workflow."
            )
            handoff_contract_hint = (
                "In handoff JSON outputs.deliverables, list the documentation artifacts you produced as a machine-readable list of paths."
            )

        worker_plan = worker_plan or {}
        deps = worker_plan.get("depends_on")
        depends_on = [str(d).strip() for d in deps] if isinstance(deps, list) else []
        responsibilities = worker_plan.get("responsibilities")
        responsibilities_list = (
            [str(item).strip() for item in responsibilities]
            if isinstance(responsibilities, list)
            else []
        )
        expected_inputs = worker_plan.get("expected_inputs")
        expected_inputs_list = (
            [str(item).strip() for item in expected_inputs]
            if isinstance(expected_inputs, list)
            else []
        )
        expected_outputs = worker_plan.get("expected_outputs")
        expected_outputs_list = (
            [str(item).strip() for item in expected_outputs]
            if isinstance(expected_outputs, list)
            else []
        )
        handoff_to = worker_plan.get("handoff_to")
        handoff_to_list = (
            [str(item).strip() for item in handoff_to]
            if isinstance(handoff_to, list)
            else []
        )
        owned_paths = worker_plan.get("owned_paths")
        owned_paths_list = (
            [str(item).strip() for item in owned_paths]
            if isinstance(owned_paths, list)
            else []
        )
        acceptance_checks = worker_plan.get("acceptance_checks")
        acceptance_checks_list = (
            [self._describe_multi_acceptance_check(item) for item in acceptance_checks if isinstance(item, dict)]
            if isinstance(acceptance_checks, list)
            else []
        )

        deps_text = ", ".join(depends_on) if depends_on else "(none)"
        responsibilities_text = (
            "\n".join(f"- {item}" for item in responsibilities_list) if responsibilities_list else "- (none)"
        )
        expected_inputs_text = (
            "\n".join(f"- {item}" for item in expected_inputs_list) if expected_inputs_list else "- (none)"
        )
        expected_outputs_text = (
            "\n".join(f"- {item}" for item in expected_outputs_list) if expected_outputs_list else "- (none)"
        )
        handoff_text = (
            ", ".join(handoff_to_list) if handoff_to_list else "(none)"
        )
        owned_paths_text = (
            "\n".join(f"- {item}" for item in owned_paths_list) if owned_paths_list else "- (none)"
        )
        acceptance_text = (
            "\n".join(f"- {item}" for item in acceptance_checks_list) if acceptance_checks_list else "- (none)"
        )
        handoff_md_path = self._multi_handoff_md_path(label)
        handoff_json_path = self._multi_handoff_json_path(label)

        return (
            "You are one worker in a LightClaw multi-agent delegation run.\n\n"
            "GLOBAL GOAL:\n"
            f"{goal}\n\n"
            "EXECUTION MODE:\n"
            "- The master orchestrator generated AGENTS.md for this run.\n"
            "- Read AGENTS.md first and follow your worker contract exactly.\n"
            "- Do not duplicate other workers' scope.\n\n"
            "TASK WORKSPACE:\n"
            f"{task_workspace_label or '(unknown)'}\n\n"
            "WORKER ROSTER:\n"
            f"{roster}\n\n"
            "YOUR LANE:\n"
            f"{label}\n\n"
            "YOUR DEPENDENCIES:\n"
            f"{deps_text}\n\n"
            "YOUR RESPONSIBILITIES:\n"
            f"{responsibilities_text}\n\n"
            "YOUR EXPECTED INPUTS:\n"
            f"{expected_inputs_text}\n\n"
            "YOUR EXPECTED OUTPUTS:\n"
            f"{expected_outputs_text}\n\n"
            "YOUR HANDOFF TARGETS:\n"
            f"{handoff_text}\n\n"
            "YOUR OWNED PATHS:\n"
            f"{owned_paths_text}\n\n"
            "YOUR ACCEPTANCE CHECKS:\n"
            f"{acceptance_text}\n\n"
            "RULES:\n"
            "- Work only on your own lane.\n"
            "- Do not wait for confirmations.\n"
            "- Make practical assumptions and implement directly.\n"
            "- Keep output concise and summarize created/updated files.\n"
            f"- Write handoff notes to `{handoff_md_path}` for downstream workers.\n"
            f"- Write machine-readable handoff JSON to `{handoff_json_path}`.\n"
            "- The handoff JSON must be raw JSON with keys: lane, status, summary, changed_files, outputs, handoff.\n"
            "- In changed_files, list only files that currently exist in the workspace and that you directly changed for this lane.\n"
            "- If owned_paths are provided, stay inside them unless the worker contract explicitly requires a broader change.\n"
            "- If your acceptance checks mention a command, assume the orchestrator will run it exactly as written.\n"
            "- If your acceptance checks mention a handoff JSON field, keep that field populated with real machine-readable data.\n"
            "- Do not output planning narrative in final answer.\n"
            f"- {handoff_contract_hint}\n"
            "- Final answer format must be:\n"
            "  1) `Summary:` one short paragraph\n"
            "  2) `Outputs:` bullet list of key files\n"
            "  3) `Handoff:` bullet list for downstream workers\n"
            f"- {lane_hint}\n"
        )

    def _build_multi_agent_repair_task(
        self,
        label: str,
        goal: str,
        workers: list[tuple[str, str]],
        worker_plan: dict[str, object] | None,
        acceptance_failures: list[str],
        previous_result: str,
        task_workspace_label: str = "",
    ) -> str:
        roster = ", ".join(f"{name}={agent}" for name, agent in workers)
        worker_plan = worker_plan or {}
        role = str(worker_plan.get("role") or "implementation").strip() or "implementation"
        owned_paths = worker_plan.get("owned_paths")
        owned_paths_list = (
            [str(item).strip() for item in owned_paths]
            if isinstance(owned_paths, list)
            else []
        )
        owned_paths_text = (
            "\n".join(f"- {item}" for item in owned_paths_list) if owned_paths_list else "- (none)"
        )
        failures_text = (
            "\n".join(f"- {item}" for item in acceptance_failures)
            if acceptance_failures
            else "- previous execution failed"
        )
        previous_excerpt = self._short_progress_text(previous_result, max_chars=1200)
        handoff_md_path = self._multi_handoff_md_path(label)
        handoff_json_path = self._multi_handoff_json_path(label)
        repair_handoff_hint = "Keep handoff JSON aligned with the final workspace state."
        if self._multi_is_backend_lane(label, role):
            repair_handoff_hint = (
                "Keep outputs.endpoints in handoff JSON aligned with the backend routes you actually serve."
            )
        elif self._multi_is_frontend_lane(label, role):
            repair_handoff_hint = (
                "Keep outputs.api_calls in handoff JSON aligned with the backend methods and paths the frontend actually calls."
            )
        elif self._multi_is_research_lane(label, role) or self._multi_is_review_lane(label, role):
            repair_handoff_hint = (
                "Keep outputs.findings in handoff JSON aligned with the actual findings, caveats, and recommendations produced by this lane."
            )
        elif self._multi_is_deliverable_lane(label, role):
            repair_handoff_hint = (
                "Keep outputs.deliverables in handoff JSON aligned with the actual artifacts produced by this lane."
            )
        return (
            "You are repairing your own lane in an existing LightClaw multi-agent run.\n\n"
            "GLOBAL GOAL:\n"
            f"{goal}\n\n"
            "TASK WORKSPACE:\n"
            f"{task_workspace_label or '(unknown)'}\n\n"
            "WORKER ROSTER:\n"
            f"{roster}\n\n"
            "YOUR LANE:\n"
            f"{label}\n\n"
            "YOUR OWNED PATHS:\n"
            f"{owned_paths_text}\n\n"
            "CURRENT FAILURES TO FIX:\n"
            f"{failures_text}\n\n"
            "PREVIOUS RESULT EXCERPT:\n"
            f"{previous_excerpt or '(none)'}\n\n"
            "REPAIR RULES:\n"
            "- Inspect the current workspace state and patch only your lane.\n"
            "- Do not restart or re-plan the whole project.\n"
            f"- Update `{handoff_md_path}` and `{handoff_json_path}` before finishing.\n"
            "- Make the acceptance failures pass with the smallest practical change.\n"
            "- If a command-based acceptance check failed, fix the workspace so that exact command passes.\n"
            "- If a handoff JSON field check failed, fix that JSON field instead of only changing prose output.\n"
            f"- {repair_handoff_hint}\n"
            "- Final answer format must stay:\n"
            "  1) `Summary:` one short paragraph\n"
            "  2) `Outputs:` bullet list of key files\n"
            "  3) `Handoff:` bullet list for downstream workers\n"
        )

    def _build_agents_plan_payload(
        self,
        goal: str,
        workers: list[tuple[str, str]],
        explicit_dependency_specs: dict[str, list[str]] | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, object]:
        labels = [label for label, _ in workers]
        docs_labels = [label for label in labels if "doc" in label.lower()]
        nondocs_labels = [label for label in labels if label not in docs_labels]

        plan_workers: list[dict[str, object]] = []
        for label, agent in workers:
            lowered = label.lower()
            role = "implementation"
            depends_on: list[str] = []
            responsibilities: list[str] = []
            expected_inputs: list[str] = []
            expected_outputs: list[str] = []
            handoff_to: list[str] = []

            if "backend" in lowered:
                role = "backend"
                responsibilities = [
                    "Implement backend API, persistence, and backend tests.",
                    "Define stable API contract and payload schemas for consumers.",
                ]
                expected_inputs = [
                    "Global goal and shared constraints from AGENTS.md.",
                ]
                expected_outputs = [
                    "Backend source code and run/test instructions.",
                    "API contract details (routes, request/response schema, ports).",
                ]
                handoff_to = [lane_label for lane_label in labels if lane_label != label]
            elif "frontend" in lowered:
                role = "frontend"
                responsibilities = [
                    "Implement frontend UI and API client integration.",
                    "Align request/response usage with backend contract.",
                ]
                expected_inputs = [
                    "API contract and constraints from AGENTS.md.",
                    "Backend handoff notes if available during the run.",
                ]
                expected_outputs = [
                    "Frontend source code, run commands, and env configuration.",
                    "UI behavior notes and integration assumptions.",
                ]
                handoff_to = [lane_label for lane_label in labels if lane_label != label]
            elif self._multi_is_research_lane(label, lowered):
                role = "research"
                responsibilities = [
                    "Research the topic, gather evidence, and capture key findings.",
                    "Produce a machine-readable findings handoff for downstream workers.",
                ]
                expected_inputs = [
                    "Global goal, scope, and constraints from AGENTS.md.",
                ]
                expected_outputs = [
                    "Research notes, evidence, and synthesized findings.",
                ]
                handoff_to = [lane_label for lane_label in labels if lane_label != label]
            elif self._multi_is_authoring_lane(label, lowered):
                role = "authoring"
                responsibilities = [
                    "Create the requested written or synthesized artifact for this lane.",
                    "Use upstream findings and constraints to produce a clear final deliverable.",
                ]
                expected_inputs = [
                    "Global goal, upstream handoffs, and workspace artifacts relevant to this deliverable.",
                ]
                expected_outputs = [
                    "Final drafted artifact and succinct handoff notes.",
                ]
                handoff_to = [lane_label for lane_label in labels if lane_label != label]
            elif self._multi_is_review_lane(label, lowered):
                role = "validation"
                depends_on = [lane_label for lane_label in labels if lane_label != label]
                responsibilities = [
                    "Review upstream outputs for gaps, contradictions, and unsupported claims.",
                    "Refine the final findings, caveats, and recommendations.",
                ]
                expected_inputs = [
                    "Upstream handoff files and generated artifacts from the workspace.",
                ]
                expected_outputs = [
                    "Validated findings, caveats, and recommendations.",
                ]
                handoff_to = []
            elif "doc" in lowered:
                role = "documentation"
                depends_on = [lane_label for lane_label in nondocs_labels if lane_label != label]
                responsibilities = [
                    "Produce consolidated project documentation.",
                    "Reflect final backend/frontend structure and usage accurately.",
                ]
                expected_inputs = [
                    "Handoff files from implementation workers.",
                    "Generated project files in this task workspace.",
                ]
                expected_outputs = [
                    "README and docs covering setup, architecture, APIs, and workflow.",
                ]
                handoff_to = []
            else:
                role = "implementation"
                responsibilities = [
                    "Implement assigned lane based on goal and AGENTS contract.",
                ]
                expected_inputs = [
                    "Global goal and dependencies in AGENTS.md.",
                ]
                expected_outputs = [
                    "Lane-specific implementation artifacts and handoff notes.",
                ]
                handoff_to = [lane_label for lane_label in labels if lane_label != label]

            owned_paths = self._normalize_multi_owned_paths(
                None,
                label=label,
                role=role,
            )
            acceptance_checks = self._normalize_multi_acceptance_checks(
                None,
                label=label,
                owned_paths=owned_paths,
                role=role,
            )
            expected_outputs = self._augment_multi_expected_outputs(
                expected_outputs,
                label=label,
                role=role,
            )

            plan_workers.append(
                {
                    "label": label,
                    "agent": agent,
                    "role": role,
                    "depends_on": depends_on,
                    "responsibilities": responsibilities,
                    "expected_inputs": expected_inputs,
                    "expected_outputs": expected_outputs,
                    "handoff_to": handoff_to,
                    "owned_paths": owned_paths,
                    "acceptance_checks": acceptance_checks,
                }
            )

        self._apply_goal_dependency_overrides(goal, plan_workers, warnings)
        self._apply_explicit_dependency_overrides(
            explicit_dependency_specs,
            plan_workers,
            warnings,
        )
        self._remove_multi_dependency_cycles(plan_workers, warnings)

        return {
            "version": 1,
            "goal": goal,
            "coordination_rules": {
                "mode": "dependency-phased-parallel",
                "shared_workspace": True,
                "handoff_dir": "handoff",
                "contract_file": "AGENTS.md",
            },
            "workers": plan_workers,
        }

    def _render_agents_markdown(self, payload: dict[str, object]) -> str:
        workers = payload.get("workers")
        workers_list = workers if isinstance(workers, list) else []

        lines = [
            "# AGENTS.md",
            "",
            "Auto-generated by LightClaw multi-agent orchestrator.",
            "",
            "## Goal",
            "",
            str(payload.get("goal") or ""),
            "",
            "## Worker Contracts",
            "",
        ]

        for item in workers_list:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            agent = str(item.get("agent") or "").strip()
            role = str(item.get("role") or "implementation").strip()
            depends_on = item.get("depends_on")
            deps = [str(d).strip() for d in depends_on] if isinstance(depends_on, list) else []
            responsibilities = item.get("responsibilities")
            resp = [str(v).strip() for v in responsibilities] if isinstance(responsibilities, list) else []
            expected_inputs = item.get("expected_inputs")
            exp_in = [str(v).strip() for v in expected_inputs] if isinstance(expected_inputs, list) else []
            expected_outputs = item.get("expected_outputs")
            exp_out = [str(v).strip() for v in expected_outputs] if isinstance(expected_outputs, list) else []
            handoff_to = item.get("handoff_to")
            handoff = [str(v).strip() for v in handoff_to] if isinstance(handoff_to, list) else []
            owned_paths = item.get("owned_paths")
            owned = [str(v).strip() for v in owned_paths] if isinstance(owned_paths, list) else []
            acceptance_checks = item.get("acceptance_checks")
            checks = (
                [self._describe_multi_acceptance_check(v) for v in acceptance_checks if isinstance(v, dict)]
                if isinstance(acceptance_checks, list)
                else []
            )

            lines.append(f"### {label}")
            lines.append(f"- agent: {agent}")
            lines.append(f"- role: {role}")
            lines.append(f"- depends_on: {', '.join(deps) if deps else '(none)'}")
            lines.append("- responsibilities:")
            lines.extend(f"  - {r}" for r in (resp or ["(none)"]))
            lines.append("- expected_inputs:")
            lines.extend(f"  - {r}" for r in (exp_in or ["(none)"]))
            lines.append("- expected_outputs:")
            lines.extend(f"  - {r}" for r in (exp_out or ["(none)"]))
            lines.append(f"- handoff_to: {', '.join(handoff) if handoff else '(none)'}")
            lines.append("- owned_paths:")
            lines.extend(f"  - {r}" for r in (owned or ["(none)"]))
            lines.append("- acceptance_checks:")
            lines.extend(f"  - {r}" for r in (checks or ["(none)"]))
            lines.append("")

        lines.append("## Machine Plan")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(payload, indent=2))
        lines.append("```")
        lines.append("")

        return "\n".join(lines)

    def _write_agents_plan_file(
        self,
        workspace: Path,
        payload: dict[str, object],
    ) -> Path:
        target = workspace / "AGENTS.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self._render_agents_markdown(payload), encoding="utf-8")
        return target

    def _load_agents_plan_file(self, workspace: Path) -> dict[str, object]:
        path = workspace / "AGENTS.md"
        if not path.exists():
            return {}
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return {}
        match = re.search(r"```json\s*([\s\S]*?)```", text)
        if not match:
            return {}
        raw = match.group(1).strip()
        try:
            obj = json.loads(raw)
        except Exception:
            return {}
        return obj if isinstance(obj, dict) else {}
