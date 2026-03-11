"""Multi-agent planning, normalization, and AGENTS.md contract helpers."""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

from ...markdown import _escape_html


class DelegationMultiPlanMixin:
    @staticmethod
    def _multi_lane_role_text(label: str, role: str) -> str:
        return f"{label} {role}".strip().lower()

    def _multi_is_backend_lane(self, label: str, role: str = "") -> bool:
        text = self._multi_lane_role_text(label, role)
        return bool(re.search(r"\b(backend|api|server|db|database|persistence|migration)\b", text))

    def _multi_is_frontend_lane(self, label: str, role: str = "") -> bool:
        text = self._multi_lane_role_text(label, role)
        return bool(re.search(r"\b(frontend|web|ui|client|react|next|vue)\b", text))

    def _multi_is_docs_lane(self, label: str, role: str = "") -> bool:
        text = self._multi_lane_role_text(label, role)
        return bool(re.search(r"\b(doc|docs|documentation|readme)\b", text))

    def _multi_is_test_lane(self, label: str, role: str = "") -> bool:
        text = self._multi_lane_role_text(label, role)
        return bool(re.search(r"\b(qa|test|testing|e2e)\b", text))

    def _multi_is_research_lane(self, label: str, role: str = "") -> bool:
        text = self._multi_lane_role_text(label, role)
        return bool(
            re.search(
                r"\b(architect|planner|planning|design|spec|research|discovery|analysis|analyst|synthesis|brief)\b",
                text,
            )
        )

    def _multi_is_review_lane(self, label: str, role: str = "") -> bool:
        text = self._multi_lane_role_text(label, role)
        return bool(re.search(r"\b(review|reviewer|validate|validation|verif|audit)\b", text))

    def _multi_is_authoring_lane(self, label: str, role: str = "") -> bool:
        text = self._multi_lane_role_text(label, role)
        return bool(
            re.search(
                r"\b(author|authoring|writer|writing|content|copy|marketing|blog|article|newsletter|report)\b",
                text,
            )
        )

    @staticmethod
    def _multi_goal_profile(goal: str) -> str:
        text = (goal or "").lower()
        if re.search(
            r"\b(build|create|code|app|api|backend|frontend|react|fastapi|python|script|tool|test|bug|fix|endpoint|database)\b",
            text,
        ):
            return "coding"
        if re.search(
            r"\b(research|analy[sz]e|analysis|investigate|compare|benchmark|audit|review|study|brief|report|findings|landscape|competitor|market)\b",
            text,
        ):
            return "analysis"
        if re.search(
            r"\b(blog|article|post|content|seo|copy|marketing|newsletter|landing page)\b",
            text,
        ):
            return "content"
        return "generic"

    def _build_fallback_multi_plan_payload(
        self,
        goal: str,
        agent_order: list[str],
    ) -> dict[str, object]:
        profile = self._multi_goal_profile(goal)
        primary = agent_order[0] if agent_order else "claude"
        secondary = agent_order[1] if len(agent_order) > 1 else primary

        if profile == "coding":
            workers = [
                {
                    "label": "builder",
                    "agent": primary,
                    "role": "implementation",
                    "depends_on": [],
                    "responsibilities": [
                        "Implement the core solution for the goal with practical defaults.",
                        "Provide runnable setup and key commands.",
                    ],
                    "expected_inputs": ["Global goal and constraints."],
                    "expected_outputs": [
                        "Primary implementation artifacts.",
                        "Notes that enable review and validation.",
                    ],
                    "handoff_to": ["reviewer"],
                },
                {
                    "label": "reviewer",
                    "agent": secondary,
                    "role": "validation",
                    "depends_on": ["builder"],
                    "responsibilities": [
                        "Validate implementation quality, edge cases, and testability.",
                        "Patch gaps or regressions discovered during review.",
                    ],
                    "expected_inputs": ["Builder outputs and handoff notes."],
                    "expected_outputs": [
                        "Validation fixes and quality checks summary.",
                    ],
                    "handoff_to": [],
                },
            ]
        elif profile == "analysis":
            workers = [
                {
                    "label": "research",
                    "agent": primary,
                    "role": "research",
                    "depends_on": [],
                    "responsibilities": [
                        "Research the topic, gather evidence, and capture key findings.",
                        "Produce a machine-readable findings handoff for downstream synthesis.",
                    ],
                    "expected_inputs": ["Global goal, scope, and evaluation criteria."],
                    "expected_outputs": [
                        "Research notes with key findings and evidence.",
                        "Machine-readable findings list in handoff JSON outputs.findings.",
                    ],
                    "handoff_to": ["reviewer"],
                },
                {
                    "label": "reviewer",
                    "agent": secondary,
                    "role": "validation",
                    "depends_on": ["research"],
                    "responsibilities": [
                        "Review findings for gaps, contradictions, and unsupported claims.",
                        "Refine the final handoff with risks, caveats, and recommendations.",
                    ],
                    "expected_inputs": ["Research handoff and source artifacts in the workspace."],
                    "expected_outputs": [
                        "Validated findings, caveats, and recommendations.",
                        "Machine-readable findings list in handoff JSON outputs.findings.",
                    ],
                    "handoff_to": [],
                },
            ]
        elif profile == "content":
            workers = [
                {
                    "label": "research",
                    "agent": primary,
                    "role": "research",
                    "depends_on": [],
                    "responsibilities": [
                        "Research best practices and relevant references for the requested content.",
                        "Produce an outline and factual guardrails.",
                    ],
                    "expected_inputs": ["Global goal and target audience."],
                    "expected_outputs": [
                        "Research notes and structured content guidance.",
                    ],
                    "handoff_to": ["author"],
                },
                {
                    "label": "author",
                    "agent": secondary,
                    "role": "authoring",
                    "depends_on": ["research"],
                    "responsibilities": [
                        "Create the final content artifact using research guidance.",
                        "Ensure readability and clear structure.",
                    ],
                    "expected_inputs": ["Research handoff and global goal."],
                    "expected_outputs": ["Final drafted artifact."],
                    "handoff_to": [],
                },
            ]
        else:
            workers = [
                {
                    "label": "executor",
                    "agent": primary,
                    "role": "implementation",
                    "depends_on": [],
                    "responsibilities": [
                        "Execute the main task requested by the goal.",
                    ],
                    "expected_inputs": ["Global goal and constraints."],
                    "expected_outputs": ["Primary solution artifacts."],
                    "handoff_to": ["validator"],
                },
                {
                    "label": "validator",
                    "agent": secondary,
                    "role": "validation",
                    "depends_on": ["executor"],
                    "responsibilities": [
                        "Validate quality, correctness, and gaps.",
                    ],
                    "expected_inputs": ["Executor outputs and handoff notes."],
                    "expected_outputs": ["Validation findings and fixes."],
                    "handoff_to": [],
                },
            ]

        for item in workers:
            label = str(item.get("label") or "").strip().lower()
            role = str(item.get("role") or "implementation").strip() or "implementation"
            item["expected_outputs"] = self._augment_multi_expected_outputs(
                item.get("expected_outputs"),
                label=label,
                role=role,
            )
            owned_paths = self._normalize_multi_owned_paths(
                item.get("owned_paths"),
                label=label,
                role=role,
            )
            item["owned_paths"] = owned_paths
            item["acceptance_checks"] = self._normalize_multi_acceptance_checks(
                item.get("acceptance_checks"),
                label=label,
                owned_paths=owned_paths,
                role=role,
            )

        return {
            "version": 1,
            "goal": goal,
            "coordination_rules": {
                "mode": "dependency-phased-parallel",
                "shared_workspace": True,
                "handoff_dir": "handoff",
                "contract_file": "AGENTS.md",
            },
            "workers": workers,
        }

    @staticmethod
    def _normalize_multi_contract_path(raw: str) -> str:
        value = str(raw or "").strip().replace("\\", "/")
        if not value:
            return ""
        value = re.sub(r"/{2,}", "/", value)
        if value.startswith("./"):
            value = value[2:]
        value = value.lstrip("/")
        if not value or value == ".." or value.startswith("../") or "/../" in value:
            return ""
        return value.rstrip("/") or ""

    def _multi_handoff_md_path(self, label: str) -> str:
        safe = self._sanitize_multi_label(label)
        return f"handoff/{safe}.md"

    def _multi_handoff_json_path(self, label: str) -> str:
        safe = self._sanitize_multi_label(label)
        return f"handoff/{safe}.json"

    def _default_multi_owned_paths(self, label: str, role: str) -> list[str]:
        text = self._multi_lane_role_text(label, role)
        if self._multi_is_backend_lane(label, role):
            return ["backend/**", "api/**", "server/**", "db/**", "migrations/**"]
        if self._multi_is_frontend_lane(label, role):
            return ["frontend/**", "web/**", "ui/**", "components/**", "pages/**", "public/**"]
        if self._multi_is_docs_lane(label, role):
            return ["README.md", "docs/**"]
        if self._multi_is_test_lane(label, role):
            return ["tests/**", "e2e/**", "qa/**"]
        if self._multi_is_research_lane(label, role):
            return ["specs/**", "research/**", "analysis/**", "reports/**", "notes/**", "outlines/**"]
        if self._multi_is_authoring_lane(label, role):
            return ["content/**", "articles/**", "posts/**", "drafts/**", "reports/**", "docs/**"]
        if self._multi_is_review_lane(label, role):
            return []
        return []

    def _normalize_multi_owned_paths(
        self,
        owned_paths_obj: object,
        label: str,
        role: str,
    ) -> list[str]:
        raw_items = owned_paths_obj if isinstance(owned_paths_obj, list) else []
        out: list[str] = []
        seen: set[str] = set()
        for item in raw_items[:8]:
            value = self._normalize_multi_contract_path(str(item or ""))
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)
        if out:
            return out

        defaults = self._default_multi_owned_paths(label, role)
        for item in defaults:
            value = self._normalize_multi_contract_path(item)
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out

    def _augment_multi_expected_outputs(
        self,
        expected_outputs_obj: object,
        label: str,
        role: str,
    ) -> list[str]:
        base = expected_outputs_obj if isinstance(expected_outputs_obj, list) else []
        out: list[str] = []
        seen: set[str] = set()

        for item in base[:8]:
            value = str(item or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)

        if self._multi_is_backend_lane(label, role):
            backend_hint = "Machine-readable endpoint list in handoff JSON outputs.endpoints."
            if backend_hint not in seen:
                seen.add(backend_hint)
                out.append(backend_hint)

        if self._multi_is_frontend_lane(label, role):
            frontend_hint = "Machine-readable API usage list in handoff JSON outputs.api_calls."
            if frontend_hint not in seen:
                seen.add(frontend_hint)
                out.append(frontend_hint)

        if self._multi_is_research_lane(label, role) or self._multi_is_review_lane(label, role):
            findings_hint = "Machine-readable findings list in handoff JSON outputs.findings."
            if findings_hint not in seen:
                seen.add(findings_hint)
                out.append(findings_hint)

        return out

    def _default_multi_acceptance_checks(
        self,
        label: str,
        owned_paths: list[str],
        role: str,
    ) -> list[dict[str, object]]:
        checks: list[dict[str, object]] = [
            {"type": "file_exists", "path": self._multi_handoff_md_path(label)},
            {"type": "handoff_json", "path": self._multi_handoff_json_path(label)},
            {"type": "reported_files_exist"},
        ]
        if self._multi_is_backend_lane(label, role):
            checks.append({"type": "json_field_nonempty", "field": "outputs.endpoints"})
        if self._multi_is_frontend_lane(label, role):
            checks.append({"type": "json_field_nonempty", "field": "outputs.api_calls"})
        if self._multi_is_research_lane(label, role) or self._multi_is_review_lane(label, role):
            checks.append({"type": "json_field_nonempty", "field": "outputs.findings"})
        if owned_paths:
            checks.append({"type": "owned_path_touched"})
            checks.append({"type": "owned_paths_only"})
        return checks

    def _normalize_multi_acceptance_checks(
        self,
        checks_obj: object,
        label: str,
        owned_paths: list[str],
        role: str,
    ) -> list[dict[str, object]]:
        raw_checks = checks_obj if isinstance(checks_obj, list) else []
        normalized: list[dict[str, object]] = []

        for item in raw_checks[:8]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "").strip().lower()
            if kind in {"reported_files_exist", "owned_path_touched", "owned_paths_only"}:
                normalized.append({"type": kind})
                continue

            if kind in {"file_exists", "handoff_json"}:
                path = self._normalize_multi_contract_path(str(item.get("path") or ""))
                if path:
                    normalized.append({"type": kind, "path": path})
                continue

            if kind == "glob_nonempty":
                pattern = self._normalize_multi_contract_path(str(item.get("pattern") or ""))
                if pattern:
                    normalized.append({"type": kind, "pattern": pattern})
                continue

            if kind == "command_succeeds":
                command = str(item.get("command") or "").strip()
                if not command:
                    continue
                normalized_check: dict[str, object] = {
                    "type": kind,
                    "command": command[:300],
                }
                cwd = self._normalize_multi_contract_path(str(item.get("cwd") or ""))
                if cwd:
                    normalized_check["cwd"] = cwd
                timeout_raw = item.get("timeout_sec")
                try:
                    timeout_sec = int(timeout_raw)
                except Exception:
                    timeout_sec = 20
                normalized_check["timeout_sec"] = max(1, min(45, timeout_sec))
                normalized.append(normalized_check)
                continue

            if kind == "json_field_nonempty":
                field = str(item.get("field") or "").strip()
                if field:
                    normalized.append({"type": kind, "field": field[:120]})
                continue

        baseline = self._default_multi_acceptance_checks(label, owned_paths, role)
        for check in baseline:
            if check not in normalized:
                normalized.append(check)
        return normalized

    @staticmethod
    def _multi_path_matches_pattern(path: str, pattern: str) -> bool:
        normalized_path = re.sub(r"/{2,}", "/", str(path or "").strip().replace("\\", "/")).lstrip("./")
        normalized_pattern = re.sub(r"/{2,}", "/", str(pattern or "").strip().replace("\\", "/")).lstrip("./")
        if not normalized_path or not normalized_pattern:
            return False
        if fnmatch.fnmatch(normalized_path, normalized_pattern):
            return True
        if normalized_pattern.endswith("/**"):
            prefix = normalized_pattern[:-3].rstrip("/")
            return normalized_path == prefix or normalized_path.startswith(prefix + "/")
        return False

    def _multi_path_matches_any(self, path: str, patterns: list[str]) -> bool:
        return any(self._multi_path_matches_pattern(path, pattern) for pattern in patterns)

    def _describe_multi_acceptance_check(self, check: dict[str, object]) -> str:
        kind = str(check.get("type") or "").strip().lower()
        if kind == "file_exists":
            return f"file exists: {check.get('path')}"
        if kind == "handoff_json":
            return f"valid handoff json: {check.get('path')}"
        if kind == "glob_nonempty":
            return f"glob has files: {check.get('pattern')}"
        if kind == "command_succeeds":
            cwd = str(check.get("cwd") or "").strip()
            command = str(check.get("command") or "").strip()
            if cwd:
                return f"command succeeds in {cwd}: {command}"
            return f"command succeeds: {command}"
        if kind == "json_field_nonempty":
            return f"handoff json field is non-empty: {check.get('field')}"
        if kind == "reported_files_exist":
            return "handoff json lists existing changed_files"
        if kind == "owned_path_touched":
            return "at least one changed file is inside owned_paths"
        if kind == "owned_paths_only":
            return "reported changed_files stay inside owned_paths"
        return kind or "unknown acceptance check"

    def _build_multi_planner_prompt(
        self,
        goal: str,
        available_agents: list[str],
        preferred_agents: list[str],
        feedback: str = "",
    ) -> str:
        preferred = ", ".join(preferred_agents) if preferred_agents else "(none)"
        feedback_text = feedback.strip() or "(none)"
        allowed_agents = ", ".join(available_agents)
        return (
            "Plan a multi-agent worker contract for this goal.\n"
            "Return ONLY JSON (no markdown/prose).\n\n"
            "Hard constraints:\n"
            "- workers count must be between 2 and 5.\n"
            f"- each worker.agent must be one of: {allowed_agents}\n"
            "- labels: lowercase, start with letter, only [a-z0-9_-], max 32 chars.\n"
            "- depends_on must reference existing labels only.\n"
            "- avoid dependency cycles.\n"
            "- maximize safe parallelism by default.\n"
            "- implementation lanes (backend/frontend/etc) should run in parallel after planning.\n"
            "- only add implementation->implementation dependencies when strictly contract-critical.\n\n"
            "- every worker must write handoff/<label>.md and handoff/<label>.json.\n"
            "- backend/API lanes should write outputs.endpoints as HTTP method/path strings like GET /api/items.\n"
            "- frontend/client lanes should write outputs.api_calls as HTTP method/path strings like GET /api/items.\n"
            "- research/analysis/review lanes should write outputs.findings as a machine-readable list of findings, caveats, or recommendations.\n"
            "- use command_succeeds only for cheap repo-local verification commands; never use installs, servers, or long-running commands.\n"
            "- use owned_paths when a worker clearly owns specific files or folders.\n"
            "- allowed acceptance_checks.type values: file_exists, glob_nonempty, command_succeeds, json_field_nonempty, handoff_json, reported_files_exist, owned_path_touched, owned_paths_only.\n\n"
            f"Goal:\n{goal}\n\n"
            f"Preferred agents order:\n{preferred}\n\n"
            f"Regeneration feedback:\n{feedback_text}\n\n"
            "Schema:\n"
            "{\n"
            '  "workers": [\n'
            "    {\n"
            '      "label": "builder",\n'
            '      "agent": "claude",\n'
            '      "role": "implementation",\n'
            '      "depends_on": [],\n'
            '      "responsibilities": ["..."],\n'
            '      "expected_inputs": ["..."],\n'
            '      "expected_outputs": ["..."],\n'
            '      "handoff_to": ["reviewer"],\n'
            '      "owned_paths": ["src/**"],\n'
            '      "acceptance_checks": [\n'
            '        {"type": "file_exists", "path": "handoff/builder.md"},\n'
            '        {"type": "command_succeeds", "command": "python -m py_compile src/main.py", "timeout_sec": 15},\n'
            '        {"type": "json_field_nonempty", "field": "outputs.findings"},\n'
            '        {"type": "handoff_json", "path": "handoff/builder.json"}\n'
            "      ]\n"
            "    }\n"
            "  ]\n"
            "}\n"
        )

    @staticmethod
    def _multi_lane_kind(item: dict[str, object]) -> str:
        label = str(item.get("label") or "").strip().lower()
        role = str(item.get("role") or "").strip().lower()
        text = f"{label} {role}"

        if re.search(r"\b(architect|planner|planning|design|spec|research|discovery|analysis|analyst|synthesis|brief)\b", text):
            return "planning"
        if re.search(r"\b(doc|docs|documentation|readme)\b", text):
            return "docs"
        if re.search(r"\b(integration|integrator|merge|compose|orchestr)\b", text):
            return "integration"
        if re.search(r"\b(review|reviewer|qa|test|testing|validate|validation|verif|e2e)\b", text):
            return "validation"
        return "implementation"

    @staticmethod
    def _is_contract_critical_lane(item: dict[str, object]) -> bool:
        label = str(item.get("label") or "").strip().lower()
        role = str(item.get("role") or "").strip().lower()
        text = f"{label} {role}"
        return bool(
            re.search(r"\b(contract|schema|interface|types?|spec|api_contract|api-contract)\b", text)
        )

    def _rebalance_multi_dependencies(
        self,
        workers: list[dict[str, object]],
        warnings: list[str],
    ) -> None:
        by_label = {
            str(item.get("label") or "").strip(): item
            for item in workers
            if str(item.get("label") or "").strip()
        }
        labels = list(by_label.keys())
        if not labels:
            return

        kinds = {label: self._multi_lane_kind(item) for label, item in by_label.items()}
        planning_labels = [label for label in labels if kinds.get(label) == "planning"]
        implementation_labels = [
            label for label in labels if kinds.get(label) == "implementation"
        ]

        def dedupe_keep_order(items: list[str]) -> list[str]:
            seen: set[str] = set()
            out: list[str] = []
            for value in items:
                if value in seen:
                    continue
                seen.add(value)
                out.append(value)
            return out

        for label in labels:
            item = by_label[label]
            kind = kinds.get(label, "implementation")
            deps_obj = item.get("depends_on")
            deps = [str(dep).strip() for dep in deps_obj] if isinstance(deps_obj, list) else []
            deps = [dep for dep in deps if dep in by_label and dep != label]
            deps = dedupe_keep_order(deps)

            if kind == "planning":
                if deps:
                    warnings.append(
                        f"Removed dependencies from planning lane `{label}` to unlock early parallel start."
                    )
                item["depends_on"] = []
                continue

            if kind == "implementation":
                dropped_impl_deps: list[str] = []
                kept: list[str] = []
                for dep in deps:
                    dep_kind = kinds.get(dep, "implementation")
                    dep_item = by_label.get(dep, {})
                    if dep_kind == "planning":
                        kept.append(dep)
                        continue
                    if dep_kind == "implementation":
                        if self._is_contract_critical_lane(dep_item):
                            kept.append(dep)
                        else:
                            dropped_impl_deps.append(dep)
                deps = dedupe_keep_order(kept)
                if dropped_impl_deps:
                    warnings.append(
                        f"Pruned non-critical implementation dependency for `{label}`: "
                        + ", ".join(dropped_impl_deps)
                    )

            if planning_labels and kind != "planning":
                for planner in planning_labels:
                    if planner != label and planner not in deps:
                        deps.append(planner)
                deps = dedupe_keep_order(deps)

            if kind in {"integration", "validation", "docs"}:
                if implementation_labels:
                    has_impl_dep = any(dep in implementation_labels for dep in deps)
                    if not has_impl_dep:
                        for dep in implementation_labels:
                            if dep != label and dep not in deps:
                                deps.append(dep)
                        deps = dedupe_keep_order(deps)
                elif not deps and planning_labels:
                    deps = [dep for dep in planning_labels if dep != label]

            item["depends_on"] = deps

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, object]:
        raw = (text or "").strip()
        if not raw:
            return {}

        fenced = re.search(r"```json\s*([\s\S]*?)```", raw, flags=re.IGNORECASE)
        if fenced:
            raw = fenced.group(1).strip()

        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            return {}
        try:
            obj = json.loads(match.group(0))
        except Exception:
            return {}
        return obj if isinstance(obj, dict) else {}

    def _normalize_multi_plan_payload(
        self,
        goal: str,
        raw_payload: dict[str, object],
        available_agents: list[str],
        agent_order: list[str],
    ) -> tuple[dict[str, object], list[tuple[str, str]], list[str], bool]:
        warnings: list[str] = []
        available_set = set(available_agents)
        fallback_used = False
        workers_raw_obj = raw_payload.get("workers")
        workers_raw = workers_raw_obj if isinstance(workers_raw_obj, list) else []
        if not workers_raw:
            fallback_used = True
            warnings.append("Planner output invalid; using fallback multi-agent template.")
            raw_payload = self._build_fallback_multi_plan_payload(goal, agent_order)
            workers_raw_obj = raw_payload.get("workers")
            workers_raw = workers_raw_obj if isinstance(workers_raw_obj, list) else []

        normalized: list[dict[str, object]] = []
        seen_labels: set[str] = set()
        agent_idx = 0

        for item in workers_raw[:5]:
            if not isinstance(item, dict):
                continue
            label = self._unique_multi_label(str(item.get("label") or "lane"), seen_labels)

            raw_agent = str(item.get("agent") or "").strip().lower()
            resolved = self._resolve_local_agent_name(raw_agent) if raw_agent else None
            if not resolved or resolved not in available_set:
                resolved = agent_order[agent_idx % len(agent_order)] if agent_order else ""
                if raw_agent:
                    warnings.append(
                        f"Worker `{label}` requested unavailable agent `{raw_agent}`; replaced with `{resolved}`."
                    )
            if not resolved:
                continue
            agent_idx += 1

            responsibilities = item.get("responsibilities")
            expected_inputs = item.get("expected_inputs")
            expected_outputs = item.get("expected_outputs")
            depends_on = item.get("depends_on")
            handoff_to = item.get("handoff_to")
            owned_paths = item.get("owned_paths")
            acceptance_checks = item.get("acceptance_checks")

            normalized.append(
                {
                    "label": label,
                    "agent": resolved,
                    "role": str(item.get("role") or "implementation").strip() or "implementation",
                    "depends_on": (
                        [str(dep).strip().lower() for dep in depends_on if str(dep).strip()]
                        if isinstance(depends_on, list)
                        else []
                    ),
                    "responsibilities": (
                        [str(v).strip() for v in responsibilities if str(v).strip()]
                        if isinstance(responsibilities, list)
                        else []
                    ),
                    "expected_inputs": (
                        [str(v).strip() for v in expected_inputs if str(v).strip()]
                        if isinstance(expected_inputs, list)
                        else []
                    ),
                    "expected_outputs": (
                        [str(v).strip() for v in expected_outputs if str(v).strip()]
                        if isinstance(expected_outputs, list)
                        else []
                    ),
                    "handoff_to": (
                        [str(v).strip().lower() for v in handoff_to if str(v).strip()]
                        if isinstance(handoff_to, list)
                        else []
                    ),
                    "owned_paths": owned_paths,
                    "acceptance_checks": acceptance_checks,
                }
            )

        if not normalized:
            fallback_used = True
            fallback_payload = self._build_fallback_multi_plan_payload(goal, agent_order)
            fallback_workers = fallback_payload.get("workers")
            normalized = [dict(item) for item in fallback_workers] if isinstance(fallback_workers, list) else []

        while len(normalized) < 2:
            label = self._unique_multi_label("validator", seen_labels)
            agent = agent_order[len(normalized) % len(agent_order)] if agent_order else ""
            if not agent:
                break
            normalized.append(
                {
                    "label": label,
                    "agent": agent,
                    "role": "validation",
                    "depends_on": [normalized[0]["label"]] if normalized else [],
                    "responsibilities": ["Validate outputs from other workers and patch gaps."],
                    "expected_inputs": ["Primary worker outputs."],
                    "expected_outputs": ["Validation fixes and notes."],
                    "handoff_to": [],
                }
            )

        normalized = normalized[:5]
        labels = [str(item.get("label") or "").strip() for item in normalized]
        label_set = set(labels)

        for item in normalized:
            label = str(item.get("label") or "").strip()
            deps = item.get("depends_on")
            dep_values = deps if isinstance(deps, list) else []
            seen_deps: set[str] = set()
            cleaned_deps: list[str] = []
            for dep in dep_values:
                dep_label = str(dep or "").strip().lower()
                if not dep_label or dep_label == label or dep_label in seen_deps:
                    continue
                if dep_label not in label_set:
                    continue
                seen_deps.add(dep_label)
                cleaned_deps.append(dep_label)
            item["depends_on"] = cleaned_deps

        self._rebalance_multi_dependencies(normalized, warnings)

        pending_map: dict[str, set[str]] = {
            str(item.get("label") or ""): set(item.get("depends_on") or [])
            for item in normalized
        }
        resolved_labels: set[str] = set()
        while pending_map:
            ready = [label for label, deps in pending_map.items() if deps <= resolved_labels]
            if not ready:
                cycle_labels = sorted(pending_map.keys())
                warnings.append(
                    "Planner dependency cycle detected and removed for: "
                    + ", ".join(cycle_labels)
                )
                for item in normalized:
                    if str(item.get("label") or "") in pending_map:
                        item["depends_on"] = []
                break
            for label in ready:
                resolved_labels.add(label)
                pending_map.pop(label, None)

        for item in normalized:
            label = str(item.get("label") or "").strip()
            if not item.get("responsibilities"):
                item["responsibilities"] = [
                    "Implement assigned lane based on goal and AGENTS contract.",
                ]
            if not item.get("expected_inputs"):
                item["expected_inputs"] = ["Global goal and dependencies in AGENTS.md."]
            if not item.get("expected_outputs"):
                item["expected_outputs"] = ["Lane-specific outputs and handoff notes."]

            handoff = item.get("handoff_to")
            handoff_values = handoff if isinstance(handoff, list) else []
            cleaned_handoff = [
                str(dep).strip().lower()
                for dep in handoff_values
                if str(dep).strip().lower() in label_set and str(dep).strip().lower() != label
            ]
            if not cleaned_handoff:
                cleaned_handoff = [candidate for candidate in labels if candidate != label]
            item["handoff_to"] = cleaned_handoff

            role = str(item.get("role") or "implementation").strip() or "implementation"
            item["expected_outputs"] = self._augment_multi_expected_outputs(
                item.get("expected_outputs"),
                label=label,
                role=role,
            )
            owned_paths = self._normalize_multi_owned_paths(
                item.get("owned_paths"),
                label=label,
                role=role,
            )
            item["owned_paths"] = owned_paths
            item["acceptance_checks"] = self._normalize_multi_acceptance_checks(
                item.get("acceptance_checks"),
                label=label,
                owned_paths=owned_paths,
                role=role,
            )

        final_workers = [
            (str(item.get("label") or "").strip(), str(item.get("agent") or "").strip())
            for item in normalized
            if str(item.get("label") or "").strip() and str(item.get("agent") or "").strip()
        ]

        payload = {
            "version": 1,
            "goal": goal,
            "coordination_rules": {
                "mode": "dependency-phased-parallel",
                "shared_workspace": True,
                "handoff_dir": "handoff",
                "contract_file": "AGENTS.md",
            },
            "workers": normalized,
        }
        return payload, final_workers, warnings, fallback_used

    async def _plan_multi_agent_payload(
        self,
        goal: str,
        available_agents: dict[str, str],
        explicit_specs: list[tuple[str, str]],
        preferred_agents: list[str],
        feedback: str = "",
    ) -> tuple[dict[str, object], str]:
        installed = sorted(available_agents.keys())
        if not installed:
            return {}, "No supported local coding agents found in PATH."

        warnings: list[str] = []
        explicit_mode = bool(explicit_specs)
        if explicit_mode:
            workers: list[tuple[str, str]] = []
            for label, raw_agent in explicit_specs:
                resolved = self._resolve_local_agent_name(raw_agent)
                if not resolved:
                    return (
                        {},
                        (
                            f"Unknown agent in <code>{_escape_html(label)}={_escape_html(raw_agent)}</code>.\n"
                            "Use one of: <code>codex</code>, <code>claude</code>."
                        ),
                    )
                if resolved not in available_agents:
                    installed_text = ", ".join(installed) if installed else "none"
                    return (
                        {},
                        (
                            f"⚠️ <code>{_escape_html(resolved)}</code> is not installed.\n"
                            f"Installed: <code>{_escape_html(installed_text)}</code>"
                        ),
                    )
                workers.append((label, resolved))
            if len(workers) < 2:
                seen: set[str] = {label for label, _ in workers}
                fallback_label = self._unique_multi_label("reviewer", seen)
                fallback_agent = workers[0][1] if workers else installed[0]
                workers.append((fallback_label, fallback_agent))
                warnings.append(
                    "Explicit roster had one worker; auto-added a reviewer lane to keep multi mode."
                )

            payload = self._build_agents_plan_payload(goal=goal, workers=workers)
            return {
                "goal": goal,
                "workers": workers,
                "plan_payload": payload,
                "warnings": warnings,
                "selection_mode": "explicit",
                "planner_mode": "explicit",
                "explicit_specs": explicit_specs,
                "preferred_agents": preferred_agents,
            }, ""

        for preferred in preferred_agents:
            if preferred not in available_agents:
                installed_text = ", ".join(installed) if installed else "none"
                return (
                    {},
                    (
                        f"⚠️ <code>{_escape_html(preferred)}</code> is not installed.\n"
                        f"Installed: <code>{_escape_html(installed_text)}</code>"
                    ),
                )

        agent_order, order_warnings = self._auto_agent_order(
            available_agents=installed,
            preferred_agents=preferred_agents,
        )
        warnings.extend(order_warnings)
        if not agent_order:
            return {}, "No available local agents could be selected for /agent multi."

        planner_prompt = self._build_multi_planner_prompt(
            goal=goal,
            available_agents=installed,
            preferred_agents=agent_order,
            feedback=feedback,
        )
        raw_payload: dict[str, object] = {}
        planner_mode = "llm"
        try:
            planner_response = await self.llm.chat(
                [{"role": "user", "content": planner_prompt}],
                system_prompt=(
                    "You are a strict JSON planner for a local multi-agent orchestrator. "
                    "Return only valid JSON."
                ),
                max_output_tokens=2400,
            )
            raw_payload = self._extract_json_object(planner_response)
        except Exception as e:
            warnings.append(f"Planner call failed ({e}); using fallback template.")
            planner_mode = "fallback"

        normalized_payload, workers, normalize_warnings, fallback_used = self._normalize_multi_plan_payload(
            goal=goal,
            raw_payload=raw_payload,
            available_agents=installed,
            agent_order=agent_order,
        )
        warnings.extend(normalize_warnings)
        if fallback_used:
            planner_mode = "fallback"

        return {
            "goal": goal,
            "workers": workers,
            "plan_payload": normalized_payload,
            "warnings": warnings,
            "selection_mode": "auto",
            "planner_mode": planner_mode,
            "explicit_specs": [],
            "preferred_agents": preferred_agents,
        }, ""

    def _render_multi_plan_preview(
        self,
        goal: str,
        workers: list[tuple[str, str]],
        plan_payload: dict[str, object],
        warnings: list[str] | None = None,
        include_confirm_hint: bool = True,
    ) -> str:
        lines = ["🤖 <b>Multi-Agent Plan Ready</b>", ""]
        lines.append(f"<b>Goal:</b> {_escape_html(goal)}")
        lines.append("")
        lines.append("<b>Worker Contracts:</b>")

        worker_contracts = plan_payload.get("workers")
        contract_list = worker_contracts if isinstance(worker_contracts, list) else []
        by_label: dict[str, dict[str, object]] = {}
        for contract in contract_list:
            if not isinstance(contract, dict):
                continue
            label = str(contract.get("label") or "").strip()
            if label:
                by_label[label] = contract

        for index, (label, agent) in enumerate(workers):
            tag = self._multi_agent_tag(label, agent, index)
            contract = by_label.get(label, {})
            role = str(contract.get("role") or "implementation").strip() or "implementation"
            depends_obj = contract.get("depends_on")
            depends_on = (
                [str(dep).strip() for dep in depends_obj]
                if isinstance(depends_obj, list)
                else []
            )
            deps_text = ", ".join(depends_on) if depends_on else "(none)"
            first_resp = ""
            responsibilities = contract.get("responsibilities")
            if isinstance(responsibilities, list):
                for item in responsibilities:
                    candidate = str(item or "").strip()
                    if candidate:
                        first_resp = candidate
                        break
            owned_paths_obj = contract.get("owned_paths")
            owned_paths = (
                [str(path).strip() for path in owned_paths_obj]
                if isinstance(owned_paths_obj, list)
                else []
            )
            lines.append(
                f"• <code>{_escape_html(tag)}</code> — role: <code>{_escape_html(role)}</code> — depends_on: <code>{_escape_html(deps_text)}</code>"
            )
            if first_resp:
                lines.append(f"  task: {_escape_html(first_resp)}")
            if owned_paths:
                owned_text = ", ".join(owned_paths[:3])
                if len(owned_paths) > 3:
                    owned_text += ", ..."
                lines.append(f"  owns: <code>{_escape_html(owned_text)}</code>")

        if warnings:
            lines.append("")
            lines.append("<b>Planner Notes:</b>")
            for warning in warnings[:6]:
                lines.append(f"• {_escape_html(warning)}")

        if include_confirm_hint:
            lines.append("")
            lines.append(
                "Confirm to run: <code>/agent multi confirm</code> (or reply <code>yes</code>)."
            )
            lines.append("Edit plan: <code>/agent multi edit &lt;feedback&gt;</code>.")
            lines.append("Cancel: <code>/agent multi cancel</code> (or reply <code>no</code>).")
        return "\n".join(lines)

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
        lane = label.lower()
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
        elif self._multi_is_docs_lane(label, role):
            lane_hint = (
                "Focus on documentation: setup, architecture, usage, and developer workflow."
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
