"""Local coding-agent delegation and doctor/check utilities."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from ..logging_setup import log
from ..markdown import _escape_html


class BotDelegationMixin:
    @staticmethod
    def _agent_aliases() -> dict[str, str]:
        return {
            "codex": "codex",
            "codex-cli": "codex",
            "claude": "claude",
            "claude-code": "claude",
        }

    def _available_local_agents(self) -> dict[str, str]:
        """Return locally available coding agents (name -> executable path)."""
        binaries = {
            "codex": "codex",
            "claude": "claude",
        }
        available: dict[str, str] = {}
        for name, binary in binaries.items():
            path = shutil.which(binary)
            if path:
                available[name] = path
        return available

    def _resolve_local_agent_name(self, raw_name: str) -> str | None:
        alias = self._agent_aliases().get((raw_name or "").strip().lower())
        if not alias:
            return None
        return alias

    @staticmethod
    def _agent_usage_text() -> str:
        return (
            "<b>Usage</b>\n"
            "<code>/agent</code> - show status + available local agents\n"
            "<code>/agent doctor</code> - run install/version/auth preflight checks\n"
            "<code>/agent use &lt;codex|claude&gt;</code> - route chat messages to that local agent\n"
            "<code>/agent off</code> - disable delegation mode for this chat\n"
            "<code>/agent run &lt;task&gt;</code> - run one task with current active agent\n"
            "<code>/agent run &lt;agent&gt; &lt;task&gt;</code> - one-shot with a specific agent\n"
            "<code>/agent multi &lt;goal&gt;</code> - auto-plan multi-agent run\n"
            "<code>/agent multi @claude @codex &lt;goal&gt;</code> - prefer specific agents\n"
            "<code>/agent multi --agent &lt;label=agent&gt; [--agent ...] &lt;goal&gt;</code> - explicit worker roster\n"
            "<code>/agent multi confirm|edit|cancel</code> - control pending multi plan"
        )

    @staticmethod
    def _multi_agent_palette() -> list[tuple[str, str]]:
        # (emoji, ANSI foreground color code for terminal chat mode)
        return [
            ("🔵", "34"),
            ("🟢", "32"),
            ("🟠", "33"),
            ("🟣", "35"),
            ("🔴", "31"),
            ("🟤", "36"),
        ]

    def _multi_agent_tag(self, label: str, agent: str, index: int) -> str:
        palette = self._multi_agent_palette()
        emoji, ansi = palette[index % len(palette)]
        plain = f"{emoji} {label}/{agent}"
        # Terminal chat supports ANSI color; Telegram does not.
        if os.getenv("LIGHTCLAW_CHAT_MODE", "").strip() == "1":
            return f"\x1b[{ansi}m{plain}\x1b[0m"
        return plain

    @staticmethod
    def _trim_wrapped_quotes(text: str) -> str:
        value = (text or "").strip()
        if len(value) >= 2 and (
            (value.startswith('"') and value.endswith('"'))
            or (value.startswith("'") and value.endswith("'"))
        ):
            return value[1:-1].strip()
        return value

    def _parse_multi_agent_args(
        self,
        tokens: list[str],
    ) -> tuple[dict[str, object], str]:
        if not tokens:
            return {}, "Usage: <code>/agent multi &lt;goal&gt;</code>."

        first = (tokens[0] or "").strip().lower()
        if first == "confirm":
            if len(tokens) > 1:
                return {}, "Usage: <code>/agent multi confirm</code>"
            return {"action": "confirm"}, ""

        if first in {"cancel", "stop"}:
            if len(tokens) > 1:
                return {}, "Usage: <code>/agent multi cancel</code>"
            return {"action": "cancel"}, ""

        if first == "edit":
            feedback = self._trim_wrapped_quotes(" ".join(tokens[1:]).strip())
            if not feedback:
                return {}, "Usage: <code>/agent multi edit &lt;feedback&gt;</code>"
            return {"action": "edit", "feedback": feedback}, ""

        specs: list[tuple[str, str]] = []
        i = 0
        while i < len(tokens):
            token = (tokens[i] or "").strip()
            if token not in {"--agent", "-a"}:
                break
            if i + 1 >= len(tokens):
                return {}, "Missing value after <code>--agent</code>."
            raw_spec = (tokens[i + 1] or "").strip()
            if "=" not in raw_spec:
                return (
                    {},
                    (
                        "Invalid agent spec: "
                        f"<code>{_escape_html(raw_spec)}</code>.\n"
                        "Use format <code>label=agent</code>, for example "
                        "<code>backend=codex</code>."
                    ),
                )
            label_raw, agent_raw = raw_spec.split("=", 1)
            label = label_raw.strip().lower()
            agent = agent_raw.strip().lower()
            if not label or not agent:
                return {}, f"Invalid agent spec: <code>{_escape_html(raw_spec)}</code>."
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", label):
                return (
                    {},
                    (
                        f"Invalid label <code>{_escape_html(label)}</code>.\n"
                        "Allowed: lowercase letters, digits, <code>_</code>, <code>-</code>, "
                        "must start with a letter."
                    ),
                )
            specs.append((label, agent))
            i += 2

        preferred_agents: list[str] = []
        goal_tokens: list[str] = []
        for token in tokens[i:]:
            value = (token or "").strip()
            if value.startswith("@") and len(value) > 1:
                alias = value[1:].strip().lower()
                canonical = self._resolve_local_agent_name(alias)
                if not canonical:
                    return (
                        {},
                        (
                            f"Unknown agent tag <code>{_escape_html(value)}</code>.\n"
                            "Use one of: <code>@codex</code>, <code>@claude</code>."
                        ),
                    )
                if canonical not in preferred_agents:
                    preferred_agents.append(canonical)
                continue
            goal_tokens.append(token)

        goal = self._trim_wrapped_quotes(" ".join(goal_tokens).strip())
        if not goal:
            return {}, "Goal is required."

        seen: set[str] = set()
        for label, _ in specs:
            if label in seen:
                return (
                    {},
                    f"Duplicate agent label: <code>{_escape_html(label)}</code>.",
                )
            seen.add(label)

        return {
            "action": "proposal",
            "goal": goal,
            "explicit_specs": specs,
            "preferred_agents": preferred_agents,
        }, ""

    @staticmethod
    def _sanitize_multi_label(raw: str) -> str:
        candidate = re.sub(r"[^a-z0-9_-]+", "-", (raw or "").strip().lower()).strip("-")
        if not candidate:
            candidate = "lane"
        if not re.match(r"^[a-z]", candidate):
            candidate = f"lane-{candidate}"
        return candidate[:32].strip("-") or "lane"

    def _unique_multi_label(self, raw: str, seen: set[str]) -> str:
        base = self._sanitize_multi_label(raw)
        if base not in seen:
            seen.add(base)
            return base
        idx = 2
        while True:
            suffix = f"-{idx}"
            trimmed = base[: max(1, 32 - len(suffix))].rstrip("-")
            candidate = f"{trimmed}{suffix}"
            if candidate not in seen:
                seen.add(candidate)
                return candidate
            idx += 1

    def _auto_agent_order(
        self,
        available_agents: list[str],
        preferred_agents: list[str] | None = None,
    ) -> tuple[list[str], list[str]]:
        available = [a for a in available_agents if a]
        available_set = set(available)
        order: list[str] = []
        warnings: list[str] = []

        for item in preferred_agents or []:
            resolved = self._resolve_local_agent_name(item) or item
            if resolved not in available_set:
                warnings.append(f"Preferred agent `{resolved}` is not installed; skipped.")
                continue
            if resolved not in order:
                order.append(resolved)

        configured_defaults = getattr(self.config, "local_agent_multi_default_agents", []) or []
        for item in configured_defaults:
            resolved = self._resolve_local_agent_name(item) or item
            if resolved not in available_set:
                warnings.append(f"Default agent `{resolved}` is not installed; skipped.")
                continue
            if resolved not in order:
                order.append(resolved)

        for item in available:
            if item not in order:
                order.append(item)

        if len(order) == 1:
            order.append(order[0])
        return order, warnings

    @staticmethod
    def _multi_goal_profile(goal: str) -> str:
        text = (goal or "").lower()
        if re.search(
            r"\b(build|create|code|app|api|backend|frontend|react|fastapi|python|script|tool|test|bug|fix|endpoint|database)\b",
            text,
        ):
            return "coding"
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
            '      "handoff_to": ["reviewer"]\n'
            "    }\n"
            "  ]\n"
            "}\n"
        )

    @staticmethod
    def _multi_lane_kind(item: dict[str, object]) -> str:
        label = str(item.get("label") or "").strip().lower()
        role = str(item.get("role") or "").strip().lower()
        text = f"{label} {role}"

        if re.search(r"\b(architect|planner|planning|design|spec|research|discovery)\b", text):
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
            lines.append(
                f"• <code>{_escape_html(tag)}</code> — role: <code>{_escape_html(role)}</code> — depends_on: <code>{_escape_html(deps_text)}</code>"
            )
            if first_resp:
                lines.append(f"  task: {_escape_html(first_resp)}")

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
        lane_hint = "Focus only on your lane and avoid unrelated files."
        if "backend" in lane:
            lane_hint = (
                "Focus on backend APIs, data models, persistence, and backend tests."
            )
        elif "frontend" in lane:
            lane_hint = (
                "Focus on frontend UI, routing/state, and integration with backend API contracts."
            )
        elif "doc" in lane:
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
            "RULES:\n"
            "- Work only on your own lane.\n"
            "- Do not wait for confirmations.\n"
            "- Make practical assumptions and implement directly.\n"
            "- Keep output concise and summarize created/updated files.\n"
            f"- Write handoff notes to `handoff/{lane}.md` for downstream workers.\n"
            "- Do not output planning narrative in final answer.\n"
            "- Final answer format must be:\n"
            "  1) `Summary:` one short paragraph\n"
            "  2) `Outputs:` bullet list of key files\n"
            "  3) `Handoff:` bullet list for downstream workers\n"
            f"- {lane_hint}\n"
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

    def _render_agent_status(self, session_id: str) -> str:
        available = self._available_local_agents()
        active = self._agent_mode_by_session.get(session_id)
        pending_multi = self._get_pending_multi_plan(session_id)

        lines = ["🤖 <b>Local Agent Delegation</b>", ""]
        if active:
            lines.append(f"<b>Active in this chat:</b> <code>{_escape_html(active)}</code>")
        else:
            lines.append("<b>Active in this chat:</b> none")
        lines.append(
            "<b>Pending multi plan:</b> "
            + ("yes" if pending_multi else "no")
        )
        lines.append("")

        if available:
            lines.append("<b>Installed local agents:</b>")
            for name in sorted(available):
                lines.append(
                    f"• <code>{_escape_html(name)}</code> "
                    f"({_escape_html(available[name])})"
                )
        else:
            lines.append("No supported local coding agents found in PATH.")

        lines.append("")
        lines.append(
            "<b>Multi defaults:</b> "
            + _escape_html(", ".join(self.config.local_agent_multi_default_agents))
        )
        lines.append(
            "<b>Multi auto-continue:</b> "
            + ("yes" if self.config.local_agent_multi_auto_continue else "no")
        )
        lines.append("")
        lines.append(self._agent_usage_text())
        return "\n".join(lines)

    @staticmethod
    def _first_nonempty_line(text: str) -> str:
        for line in (text or "").splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
        return ""

    @staticmethod
    def _format_age(seconds: float) -> str:
        if seconds < 60:
            return f"{int(seconds)}s"
        if seconds < 3600:
            return f"{int(seconds // 60)}m"
        if seconds < 86400:
            return f"{int(seconds // 3600)}h"
        return f"{int(seconds // 86400)}d"

    def _run_probe_command(
        self,
        cmd: list[str],
        timeout_sec: int = 8,
        input_text: str | None = None,
    ) -> dict:
        """Run a short-lived local CLI probe command."""
        env = os.environ.copy()
        env["CI"] = "1"
        try:
            completed = subprocess.run(
                cmd,
                input=input_text,
                text=True,
                capture_output=True,
                timeout=max(1, int(timeout_sec)),
                env=env,
            )
            return {
                "ok": completed.returncode == 0,
                "exit_code": int(completed.returncode),
                "stdout": str(completed.stdout or ""),
                "stderr": str(completed.stderr or ""),
                "timed_out": False,
                "error": "",
            }
        except subprocess.TimeoutExpired as e:
            return {
                "ok": False,
                "exit_code": 124,
                "stdout": str(e.stdout or ""),
                "stderr": str(e.stderr or ""),
                "timed_out": True,
                "error": f"timed out after {int(timeout_sec)}s",
            }
        except Exception as e:
            return {
                "ok": False,
                "exit_code": 1,
                "stdout": "",
                "stderr": "",
                "timed_out": False,
                "error": str(e),
            }

    def _probe_agent_version(self, agent: str) -> str:
        binary = {"codex": "codex", "claude": "claude"}[agent]
        probe = self._run_probe_command([binary, "--version"], timeout_sec=6)
        merged = self._strip_ansi(
            "\n".join(part for part in [probe.get("stdout", ""), probe.get("stderr", "")] if part)
        )
        line = self._first_nonempty_line(merged)
        if line:
            return line[:200]
        if probe.get("timed_out"):
            return "version check timed out"
        if probe.get("error"):
            return f"version check failed: {probe['error'][:120]}"
        return "unknown"

    @staticmethod
    def _resolve_codex_auth_path() -> Path:
        codex_home = os.getenv("CODEX_HOME", "").strip()
        if codex_home:
            return Path(codex_home).expanduser() / "auth.json"
        return Path.home() / ".codex" / "auth.json"

    @staticmethod
    def _resolve_claude_settings_paths() -> list[Path]:
        return [
            Path.home() / ".claude" / "settings.json",
            Path.home() / ".config" / "claude" / "settings.json",
        ]

    def _codex_doctor_auth_status(self) -> tuple[str, str, str]:
        auth_path = self._resolve_codex_auth_path()
        token_present = False
        path_parse_error = False
        age_seconds = 0.0
        age_known = False

        if auth_path.exists():
            try:
                payload = json.loads(auth_path.read_text(encoding="utf-8"))
                tokens = payload.get("tokens") if isinstance(payload, dict) else {}
                access_token = tokens.get("access_token") if isinstance(tokens, dict) else ""
                token_present = isinstance(access_token, str) and bool(access_token.strip())
            except Exception:
                path_parse_error = True

            try:
                age_seconds = max(0.0, time.time() - auth_path.stat().st_mtime)
                age_known = True
            except Exception:
                age_known = False

        login_probe = self._run_probe_command(["codex", "login", "status"], timeout_sec=8)
        login_text = self._strip_ansi(
            "\n".join(
                part
                for part in [login_probe.get("stdout", ""), login_probe.get("stderr", "")]
                if part
            )
        ).strip()
        login_text_lower = login_text.lower()
        logged_in = "logged in" in login_text_lower and "not logged" not in login_text_lower

        age_note = ""
        if age_known:
            age_note = f" (auth file age: {self._format_age(age_seconds)})"

        if logged_in and token_present:
            if age_known and age_seconds > 3600:
                return (
                    "warn",
                    f"Logged in, but auth file may be stale (>1h){age_note}.",
                    "codex login",
                )
            return (
                "ok",
                f"Logged in and access token found at {auth_path.as_posix()}{age_note}.",
                "",
            )

        if token_present and not logged_in:
            return (
                "warn",
                f"Token exists at {auth_path.as_posix()}, but login status probe was unclear.",
                "codex login status",
            )

        if path_parse_error:
            return (
                "warn",
                f"Could not parse {auth_path.as_posix()}.",
                "codex login",
            )

        if login_probe.get("timed_out"):
            return (
                "warn",
                "Login status probe timed out and no token file was found.",
                "codex login",
            )

        status_hint = self._first_nonempty_line(login_text)
        if status_hint:
            status_hint = f" ({status_hint[:120]})"
        return (
            "error",
            f"No valid Codex login detected{status_hint}.",
            "codex login",
        )

    def _claude_doctor_auth_status(self) -> tuple[str, str, str]:
        token_keys = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        for key in token_keys:
            if os.getenv(key, "").strip():
                return ("ok", f"{key} is set in process environment.", "")

        parse_errors: list[str] = []
        for settings_path in self._resolve_claude_settings_paths():
            if not settings_path.exists():
                continue
            try:
                data = json.loads(settings_path.read_text(encoding="utf-8"))
            except Exception:
                parse_errors.append(settings_path.as_posix())
                continue
            env_block = data.get("env") if isinstance(data, dict) else None
            if not isinstance(env_block, dict):
                continue
            for key in token_keys:
                value = env_block.get(key)
                if isinstance(value, str) and value.strip():
                    return ("ok", f"{key} found in {settings_path.as_posix()}.", "")

        if parse_errors:
            paths = ", ".join(parse_errors[:2])
            return (
                "warn",
                f"Could not parse Claude settings file(s): {paths}.",
                "claude setup-token",
            )

        return (
            "error",
            "No Claude auth token detected in env or Claude settings.",
            "claude setup-token",
        )

    def _render_agent_doctor_report(self) -> str:
        """Run local delegation preflight checks for supported external agent CLIs."""
        available = self._available_local_agents()
        auth_checks = {
            "codex": self._codex_doctor_auth_status,
            "claude": self._claude_doctor_auth_status,
        }

        lines = [
            "🩺 <b>Local Agent Doctor</b>",
            "",
            "Legend: ✅ ready, ⚠️ attention needed, ❌ action required",
            "",
        ]

        for agent in ("codex", "claude"):
            path = available.get(agent)
            if not path:
                lines.append(f"❌ <b>{_escape_html(agent)}</b>")
                lines.append("• Installed: no (not found in PATH)")
                lines.append(f"• Fix: install <code>{_escape_html(agent)}</code> and ensure it is on PATH")
                lines.append("")
                continue

            version = self._probe_agent_version(agent)
            status, auth_msg, fix = auth_checks[agent]()
            status_icon = {"ok": "✅", "warn": "⚠️", "error": "❌"}.get(status, "❓")

            lines.append(f"{status_icon} <b>{_escape_html(agent)}</b>")
            lines.append(f"• Path: <code>{_escape_html(path)}</code>")
            lines.append(f"• Version: <code>{_escape_html(version)}</code>")
            lines.append(f"• Auth: {_escape_html(auth_msg)}")
            if fix:
                lines.append(f"• Fix: <code>{_escape_html(fix)}</code>")
            lines.append("")

        lines.append("Run this before <code>/agent use ...</code> when delegation fails.")
        return "\n".join(lines).strip()

    @staticmethod
    def _slugify_goal_name(text: str, max_len: int = 56) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
        if not slug:
            return "task"
        slug = slug[:max_len].strip("-")
        return slug or "task"

    def _create_task_workspace(self, goal_text: str) -> Path:
        root = Path(self.config.workspace_path).resolve()
        root.mkdir(parents=True, exist_ok=True)

        stamp = time.strftime("%Y%m%d_%H%M%S")
        slug = self._slugify_goal_name(goal_text)
        base_name = f"{stamp}_{slug}"
        candidate = root / base_name
        idx = 2
        while candidate.exists():
            candidate = root / f"{base_name}_{idx}"
            idx += 1
        candidate.mkdir(parents=True, exist_ok=False)
        return candidate

    def _workspace_rel_label(self, workspace: Path) -> str:
        root = Path(self.config.workspace_path).resolve()
        try:
            return workspace.resolve().relative_to(root).as_posix()
        except Exception:
            return workspace.resolve().as_posix()

    def _build_delegation_prompt(self, task: str, workspace: Path | None = None) -> str:
        target_workspace = (workspace or Path(self.config.workspace_path).resolve()).resolve()
        workspace_path = target_workspace.as_posix()
        return (
            "You are a local coding agent delegated by LightClaw.\n"
            f"Workspace root: {workspace_path}\n\n"
            "Requirements:\n"
            "- Implement the task directly by creating/editing files in this workspace.\n"
            "- Do not ask for confirmation; make reasonable assumptions and proceed.\n"
            "- If the task is large, still perform as much as possible in one run.\n"
            "- Do not dump full source files in the final response.\n"
            "- End with a concise summary of what was created/updated.\n\n"
            "TASK:\n"
            f"{task}\n"
        )

    def _snapshot_workspace_state(
        self,
        workspace: Path | None = None,
    ) -> dict[str, tuple[int, int]]:
        """Snapshot workspace file metadata for before/after change detection."""
        workspace = (workspace or Path(self.config.workspace_path).resolve()).resolve()
        snapshot: dict[str, tuple[int, int]] = {}
        for path in workspace.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except Exception:
                continue
            rel = path.relative_to(workspace).as_posix()
            snapshot[rel] = (int(stat.st_size), int(stat.st_mtime_ns))
        return snapshot

    @staticmethod
    def _summarize_workspace_delta(
        before: dict[str, tuple[int, int]],
        after: dict[str, tuple[int, int]],
        max_items_per_group: int = 12,
    ) -> str:
        before_paths = set(before.keys())
        after_paths = set(after.keys())

        created = sorted(after_paths - before_paths)
        deleted = sorted(before_paths - after_paths)
        updated = sorted(
            path for path in (before_paths & after_paths) if before[path] != after[path]
        )

        total = len(created) + len(updated) + len(deleted)
        if total == 0:
            return "No workspace file changes detected."

        lines = [
            "✅ Workspace changes detected:",
            f"- Created: {len(created)}",
            f"- Updated: {len(updated)}",
            f"- Deleted: {len(deleted)}",
        ]

        for label, items in (("Created", created), ("Updated", updated), ("Deleted", deleted)):
            if not items:
                continue
            for path in items[:max_items_per_group]:
                lines.append(f"- {label}: `{path}`")
            remaining = len(items) - max_items_per_group
            if remaining > 0:
                lines.append(f"- {label}: ... and {remaining} more")

        return "\n".join(lines)

    @staticmethod
    def _strip_ansi(text: str) -> str:
        return re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", text or "")

    @staticmethod
    def _compact_external_agent_summary(text: str, max_chars: int = 900) -> str:
        raw = (text or "").strip()
        if not raw:
            return ""
        compact = re.sub(r"```[\s\S]*?```", "", raw)
        compact = re.sub(r"\n{3,}", "\n\n", compact).strip()
        if len(compact) > max_chars:
            compact = compact[:max_chars].rstrip() + "..."
        return compact

    @staticmethod
    def _strip_markdown_links(text: str) -> str:
        return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text or "")

    @staticmethod
    def _delegation_result_state(result_text: str) -> str:
        text = (result_text or "")
        if "⚠️ Timed out" in text:
            return "timed_out"
        if "⚠️ Worker failed:" in text:
            return "failed"
        if "⚠️ `" in text and "exited with code" in text:
            return "failed"
        if "⚠️ Skipped" in text:
            return "skipped"
        if "✅ Finished in " in text:
            return "success"
        return "unknown"

    def _extract_delegation_highlight(self, result_text: str, max_chars: int = 280) -> str:
        raw = self._strip_markdown_links(self._strip_ansi(result_text))
        if not raw.strip():
            return ""

        summary_match = re.search(
            r"(?ims)^Summary:\s*(.+?)(?:^\w[^:\n]{0,40}:\s*$|\Z)",
            raw,
        )
        if summary_match:
            summary_text = re.sub(r"\s+", " ", summary_match.group(1)).strip()
            return self._short_progress_text(summary_text, max_chars=max_chars)

        ignored_prefixes = (
            "🤖 Delegated to",
            "📁 Task workspace:",
            "✅ Finished in ",
            "⚠️ ",
            "✅ Workspace changes detected:",
            "- Created:",
            "- Updated:",
            "- Deleted:",
            "stderr:",
            "Outputs:",
            "Handoff:",
        )

        informative: list[str] = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if any(stripped.startswith(prefix) for prefix in ignored_prefixes):
                continue
            if stripped.startswith("- ") or stripped.startswith("• "):
                continue
            informative.append(stripped)
            if len(informative) >= 2:
                break

        merged = " ".join(informative).strip()
        return self._short_progress_text(merged, max_chars=max_chars) if merged else ""

    def _extract_workspace_label_from_result(self, result_text: str) -> str:
        match = re.search(r"(?m)^📁 Task workspace:\s*`?([^`\n]+)`?\s*$", result_text or "")
        if not match:
            return ""
        return str(match.group(1) or "").strip()

    def _build_single_delegation_memory_entry(
        self,
        agent: str,
        task: str,
        result_text: str,
        workspace_label: str = "",
    ) -> str:
        state = self._delegation_result_state(result_text)
        workspace = workspace_label.strip() or self._extract_workspace_label_from_result(result_text)
        highlight = self._extract_delegation_highlight(result_text, max_chars=320)
        task_text = self._short_progress_text(task, max_chars=260)

        lines = [
            "[delegation-context]",
            "mode: single",
            f"agent: {agent}",
            f"status: {state}",
            f"task: {task_text}",
        ]
        if workspace:
            lines.append(f"workspace: {workspace}")
        if highlight:
            lines.append(f"highlight: {highlight}")
        return "\n".join(lines)

    def _build_multi_delegation_memory_entry(
        self,
        goal: str,
        workspace_label: str,
        workers: list[tuple[str, str]],
        results_by_label: dict[str, object],
    ) -> str:
        lines = [
            "[delegation-context]",
            "mode: multi",
            f"goal: {self._short_progress_text(goal, max_chars=260)}",
            f"workspace: {workspace_label}",
            "workers:",
        ]

        for label, agent in workers:
            result_text = str(results_by_label.get(label, ""))
            state = self._delegation_result_state(result_text)
            lines.append(f"- {label}/{agent}: {state}")
            highlight = self._extract_delegation_highlight(result_text, max_chars=220)
            if highlight:
                lines.append(f"  highlight: {highlight}")

        return "\n".join(lines)

    def _parse_codex_exec_output(self, stdout: str) -> str:
        parts: list[str] = []
        last_error = ""
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            event_type = str(obj.get("type") or "")
            if event_type == "item.completed":
                item = obj.get("item") or {}
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text = str(item.get("text") or "").strip()
                    if text:
                        parts.append(text)
            elif event_type == "error":
                last_error = str(obj.get("message") or last_error)
            elif event_type == "turn.failed":
                err = obj.get("error") or {}
                if isinstance(err, dict):
                    last_error = str(err.get("message") or last_error)

        if parts:
            # Codex streams interim messages; keep only the final assistant message.
            return parts[-1].strip()
        if last_error:
            return f"Error: {last_error}"
        return (stdout or "").strip()[-2000:]

    def _parse_claude_cli_output(self, stdout: str) -> str:
        cleaned = self._strip_ansi(stdout).strip()
        if not cleaned:
            return ""

        parsed_obj = None
        try:
            parsed_obj = json.loads(cleaned)
        except Exception:
            for line in reversed(cleaned.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed_obj = json.loads(line)
                    break
                except Exception:
                    continue

        if isinstance(parsed_obj, dict):
            result = str(parsed_obj.get("result") or "").strip()
            if result:
                return result
            msg = str(parsed_obj.get("message") or "").strip()
            if msg:
                return msg

        return cleaned[-2000:]

    def _build_local_agent_command(
        self,
        agent: str,
        workspace: Path,
        prompt: str,
        stream_output: bool,
    ) -> tuple[list[str], str | None]:
        run_input: str | None = None
        if agent == "codex":
            cmd = [
                "codex",
                "exec",
                "--json",
                "--ephemeral",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
                "--color",
                "never",
                "-C",
                workspace.as_posix(),
                "-",
            ]
            run_input = prompt
            return cmd, run_input

        if agent == "claude":
            cmd = [
                "claude",
                "-p",
                "--dangerously-skip-permissions",
                "--no-chrome",
                "--no-session-persistence",
                "-",
            ]
            if stream_output:
                cmd.extend(
                    [
                        "--output-format",
                        "stream-json",
                        "--include-partial-messages",
                        "--verbose",
                    ]
                )
            else:
                cmd.extend(["--output-format", "json"])
            run_input = prompt
            return cmd, run_input

        return [], run_input

    @staticmethod
    def _short_progress_text(text: str, max_chars: int = 180) -> str:
        cleaned = re.sub(r"\s+", " ", (text or "").strip())
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[: max_chars - 3].rstrip() + "..."

    def _new_progress_state(self) -> dict[str, object]:
        now = time.monotonic()
        return {
            "last_event_at": now,
            "reasoning_count": 0,
            "tool_calls": 0,
            "commands_total": 0,
            "commands_failed": 0,
            "errors": 0,
            "last_reasoning": "",
            "last_activity": "starting delegated run",
            "last_output": "",
        }

    def _ingest_codex_progress_obj(self, obj: dict, state: dict[str, object]):
        event_type = str(obj.get("type") or "")

        if event_type == "item.started":
            item = obj.get("item") or {}
            if isinstance(item, dict) and str(item.get("type") or "") == "command_execution":
                cmd = self._short_progress_text(str(item.get("command") or ""))
                if cmd:
                    state["last_activity"] = f"running command: {cmd}"
            return

        if event_type == "item.completed":
            item = obj.get("item") or {}
            if not isinstance(item, dict):
                return
            item_type = str(item.get("type") or "")

            if item_type == "reasoning":
                text = self._short_progress_text(str(item.get("text") or ""), max_chars=220)
                if text:
                    state["last_reasoning"] = text
                state["reasoning_count"] = int(state.get("reasoning_count", 0)) + 1
                state["last_activity"] = "reasoning update"
                return

            if item_type == "command_execution":
                state["commands_total"] = int(state.get("commands_total", 0)) + 1
                exit_code_raw = item.get("exit_code")
                exit_code = exit_code_raw if isinstance(exit_code_raw, int) else 0
                cmd = self._short_progress_text(str(item.get("command") or ""))
                if exit_code != 0:
                    state["commands_failed"] = int(state.get("commands_failed", 0)) + 1
                    state["last_activity"] = (
                        f"command failed: {cmd}" if cmd else f"command failed (exit {exit_code})"
                    )
                else:
                    state["last_activity"] = (
                        f"command finished: {cmd}" if cmd else "command finished"
                    )
                return

            if item_type == "agent_message":
                text = self._short_progress_text(str(item.get("text") or ""), max_chars=220)
                if text:
                    state["last_output"] = text
                    state["last_activity"] = "agent response update"
                return

        if event_type in {"error", "turn.failed"}:
            state["errors"] = int(state.get("errors", 0)) + 1
            msg = self._short_progress_text(str(obj.get("message") or "agent runtime error"))
            if msg:
                state["last_activity"] = msg

    def _ingest_claude_progress_obj(self, obj: dict, state: dict[str, object]):
        obj_type = str(obj.get("type") or "")

        if obj_type == "stream_event":
            event = obj.get("event") or {}
            if not isinstance(event, dict):
                return
            event_type = str(event.get("type") or "")

            if event_type == "content_block_start":
                block = event.get("content_block") or {}
                if isinstance(block, dict):
                    block_type = str(block.get("type") or "")
                    if block_type == "tool_use":
                        state["tool_calls"] = int(state.get("tool_calls", 0)) + 1
                        tool_name = self._short_progress_text(str(block.get("name") or "tool"))
                        state["last_activity"] = f"using tool: {tool_name}"
                    elif block_type == "text":
                        state["last_activity"] = "drafting response"
                return

            if event_type == "content_block_delta":
                delta = event.get("delta") or {}
                if isinstance(delta, dict) and str(delta.get("type") or "") == "text_delta":
                    text = self._short_progress_text(str(delta.get("text") or ""), max_chars=200)
                    if text:
                        state["last_output"] = text
                        state["last_activity"] = "drafting response"
                return

            if event_type == "message_delta":
                delta = event.get("delta") or {}
                if isinstance(delta, dict):
                    stop_reason = str(delta.get("stop_reason") or "")
                    if stop_reason == "tool_use":
                        state["last_activity"] = "waiting for tool result"
                return

        if obj_type == "assistant":
            msg = obj.get("message") or {}
            if not isinstance(msg, dict):
                return
            content = msg.get("content")
            if not isinstance(content, list):
                return
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type") or "")
                if block_type == "tool_use":
                    state["tool_calls"] = int(state.get("tool_calls", 0)) + 1
                    tool_name = self._short_progress_text(str(block.get("name") or "tool"))
                    state["last_activity"] = f"using tool: {tool_name}"
                elif block_type == "text":
                    text = self._short_progress_text(str(block.get("text") or ""), max_chars=220)
                    if text:
                        state["last_output"] = text
                        state["last_activity"] = "response update"
            return

        if obj_type == "user" and isinstance(obj.get("tool_use_result"), dict):
            state["last_activity"] = "received tool result"
            return

        if obj_type == "result":
            text = self._short_progress_text(str(obj.get("result") or ""), max_chars=220)
            if text:
                state["last_output"] = text
                state["last_activity"] = "finalizing response"
            return

        if obj_type == "error":
            state["errors"] = int(state.get("errors", 0)) + 1
            state["last_activity"] = self._short_progress_text(
                str(obj.get("message") or "claude runtime error")
            )

    def _ingest_progress_event(
        self,
        agent: str,
        raw_line: str,
        state: dict[str, object],
        stream_name: str,
    ):
        line = self._strip_ansi(raw_line or "").strip()
        if not line:
            return

        state["last_event_at"] = time.monotonic()
        if stream_name == "stderr":
            state["last_activity"] = self._short_progress_text(line, max_chars=220)
            return

        try:
            obj = json.loads(line)
        except Exception:
            state["last_activity"] = self._short_progress_text(line, max_chars=220)
            return

        if not isinstance(obj, dict):
            return

        if agent == "codex":
            self._ingest_codex_progress_obj(obj, state)
            return
        if agent == "claude":
            self._ingest_claude_progress_obj(obj, state)
            return

    def _render_progress_summary(
        self,
        agent: str,
        state: dict[str, object],
        elapsed: float,
        heartbeat: bool,
    ) -> str:
        lines = [f"⏳ {agent} is still working ({int(elapsed)}s elapsed)."]
        progress_parts: list[str] = []

        reasoning_count = int(state.get("reasoning_count", 0))
        tool_calls = int(state.get("tool_calls", 0))
        commands_total = int(state.get("commands_total", 0))
        commands_failed = int(state.get("commands_failed", 0))
        errors_seen = int(state.get("errors", 0))

        if reasoning_count > 0:
            progress_parts.append(f"reasoning updates: {reasoning_count}")
        if tool_calls > 0:
            progress_parts.append(f"tool calls: {tool_calls}")
        if commands_total > 0:
            if commands_failed > 0:
                progress_parts.append(f"commands: {commands_total} ({commands_failed} failed)")
            else:
                progress_parts.append(f"commands: {commands_total}")
        if progress_parts:
            lines.append("- Progress: " + ", ".join(progress_parts))

        last_reasoning = self._short_progress_text(str(state.get("last_reasoning", "")), 220)
        if last_reasoning:
            lines.append(f"- Latest reasoning: {last_reasoning}")

        last_activity = self._short_progress_text(str(state.get("last_activity", "")), 220)
        if last_activity:
            lines.append(f"- Latest activity: {last_activity}")

        if not last_reasoning:
            last_output = self._short_progress_text(str(state.get("last_output", "")), 220)
            if last_output:
                lines.append(f"- Latest output: {last_output}")

        last_event_at = float(state.get("last_event_at", time.monotonic()))
        idle_for = max(0, int(time.monotonic() - last_event_at))
        if heartbeat and idle_for > 0:
            lines.append(f"- Heartbeat: no new events for {idle_for}s, process still running.")

        if errors_seen > 0:
            lines.append(f"- Errors seen in stream: {errors_seen}")

        summary = "\n".join(lines).strip()
        if len(summary) > 1200:
            summary = summary[:1197].rstrip() + "..."
        return summary

    async def _invoke_local_agent_streaming(
        self,
        agent: str,
        task: str,
        workspace: Path | None = None,
        progress_cb: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict:
        workspace = (workspace or Path(self.config.workspace_path).resolve()).resolve()
        timeout_sec = max(60, int(self.config.local_agent_timeout_sec))
        progress_interval = max(10, int(self.config.local_agent_progress_interval_sec))
        prompt = self._build_delegation_prompt(task, workspace=workspace)
        env = os.environ.copy()
        env["LIGHTCLAW_DELEGATED_AGENT"] = "1"
        env["CI"] = "1"

        cmd, run_input = self._build_local_agent_command(
            agent=agent,
            workspace=workspace,
            prompt=prompt,
            stream_output=True,
        )
        if not cmd:
            return {
                "ok": False,
                "exit_code": 1,
                "stdout": "",
                "stderr": f"unsupported local agent: {agent}",
                "summary": "",
                "elapsed": 0.0,
                "timed_out": False,
            }

        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if run_input is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace.as_posix(),
                env=env,
            )
        except Exception as e:
            return {
                "ok": False,
                "exit_code": 1,
                "stdout": "",
                "stderr": str(e),
                "summary": "",
                "elapsed": 0.0,
                "timed_out": False,
            }

        if run_input is not None and proc.stdin:
            try:
                proc.stdin.write(run_input.encode("utf-8"))
                await proc.stdin.drain()
            except Exception:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        state = self._new_progress_state()
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        heartbeat_stop = asyncio.Event()

        async def emit_progress(text: str):
            if not progress_cb:
                return
            try:
                await progress_cb(text)
            except Exception:
                # Progress updates are best-effort and must not fail delegation.
                pass

        async def _iter_stream_lines(stream):
            # Avoid StreamReader.readline() hard-limit failures on very long JSON lines
            # (e.g. Claude stream-json events with large content blocks).
            pending = b""
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                pending += chunk

                while True:
                    newline_idx = pending.find(b"\n")
                    if newline_idx < 0:
                        break
                    raw_line = pending[:newline_idx]
                    pending = pending[newline_idx + 1 :]
                    if raw_line.endswith(b"\r"):
                        raw_line = raw_line[:-1]
                    yield raw_line.decode("utf-8", errors="replace")

            if pending:
                if pending.endswith(b"\r"):
                    pending = pending[:-1]
                yield pending.decode("utf-8", errors="replace")

        parse_warning_emitted: set[str] = set()

        async def read_stream(stream, collector: list[str], stream_name: str):
            if stream is None:
                return
            async for line in _iter_stream_lines(stream):
                collector.append(line)
                try:
                    self._ingest_progress_event(agent, line, state, stream_name)
                except Exception as e:
                    # Progress parsing is best-effort; never crash the worker on it.
                    state["errors"] = int(state.get("errors", 0)) + 1
                    state["last_activity"] = self._short_progress_text(
                        f"progress parser warning: {e}",
                        max_chars=220,
                    )
                    if stream_name not in parse_warning_emitted:
                        parse_warning_emitted.add(stream_name)
                        log.warning(
                            f"Delegation progress parse warning for {agent} {stream_name}: {e}"
                        )

        async def heartbeat_loop():
            while not heartbeat_stop.is_set():
                try:
                    await asyncio.wait_for(heartbeat_stop.wait(), timeout=progress_interval)
                    return
                except asyncio.TimeoutError:
                    await emit_progress(
                        self._render_progress_summary(
                            agent=agent,
                            state=state,
                            elapsed=time.monotonic() - started,
                            heartbeat=True,
                        )
                    )

        heartbeat_task = (
            asyncio.create_task(heartbeat_loop()) if progress_cb else None
        )

        timed_out = False
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    read_stream(proc.stdout, stdout_lines, "stdout"),
                    read_stream(proc.stderr, stderr_lines, "stderr"),
                    proc.wait(),
                ),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            timed_out = True
            try:
                proc.kill()
            except Exception:
                pass
            await proc.wait()
            stderr_lines.append(f"Timed out after {timeout_sec}s")
        finally:
            heartbeat_stop.set()
            if heartbeat_task:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass

        elapsed = time.monotonic() - started
        exit_code = 124 if timed_out else int(proc.returncode if proc.returncode is not None else 1)
        stdout = "\n".join(stdout_lines)
        stderr = "\n".join(stderr_lines)

        if agent == "codex":
            summary = self._parse_codex_exec_output(stdout)
        else:
            summary = self._parse_claude_cli_output(stdout)

        ok = exit_code == 0
        if summary.strip().lower().startswith("error:"):
            ok = False

        return {
            "ok": ok,
            "exit_code": int(exit_code),
            "stdout": stdout,
            "stderr": stderr,
            "summary": summary,
            "elapsed": elapsed,
            "timed_out": timed_out,
        }

    def _invoke_local_agent_sync(
        self,
        agent: str,
        task: str,
        workspace: Path | None = None,
    ) -> dict:
        workspace = (workspace or Path(self.config.workspace_path).resolve()).resolve()
        timeout_sec = max(60, int(self.config.local_agent_timeout_sec))
        prompt = self._build_delegation_prompt(task, workspace=workspace)
        env = os.environ.copy()
        env["LIGHTCLAW_DELEGATED_AGENT"] = "1"
        env["CI"] = "1"

        cmd, run_input = self._build_local_agent_command(
            agent=agent,
            workspace=workspace,
            prompt=prompt,
            stream_output=False,
        )
        if not cmd:
            return {
                "ok": False,
                "exit_code": 1,
                "stdout": "",
                "stderr": f"unsupported local agent: {agent}",
                "summary": "",
                "elapsed": 0.0,
                "timed_out": False,
            }

        started = time.monotonic()
        try:
            completed = subprocess.run(
                cmd,
                input=run_input,
                text=True,
                capture_output=True,
                cwd=workspace.as_posix(),
                env=env,
                timeout=timeout_sec,
            )
            elapsed = time.monotonic() - started
        except subprocess.TimeoutExpired as e:
            elapsed = time.monotonic() - started
            return {
                "ok": False,
                "exit_code": 124,
                "stdout": str(e.stdout or ""),
                "stderr": (str(e.stderr or "") + f"\nTimed out after {timeout_sec}s").strip(),
                "summary": "",
                "elapsed": elapsed,
                "timed_out": True,
            }
        except Exception as e:
            elapsed = time.monotonic() - started
            return {
                "ok": False,
                "exit_code": 1,
                "stdout": "",
                "stderr": str(e),
                "summary": "",
                "elapsed": elapsed,
                "timed_out": False,
            }

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""

        if agent == "codex":
            summary = self._parse_codex_exec_output(stdout)
        else:
            summary = self._parse_claude_cli_output(stdout)

        ok = completed.returncode == 0
        if summary.strip().lower().startswith("error:"):
            ok = False

        return {
            "ok": ok,
            "exit_code": int(completed.returncode if ok or completed.returncode != 0 else 1),
            "stdout": stdout,
            "stderr": stderr,
            "summary": summary,
            "elapsed": elapsed,
            "timed_out": False,
        }

    async def _run_local_agent_task(
        self,
        session_id: str,
        agent: str,
        task: str,
        progress_cb: Callable[[str], Awaitable[None]] | None = None,
        include_workspace_delta: bool = True,
        workspace_dir: Path | str | None = None,
    ) -> str:
        available = self._available_local_agents()
        if agent not in available:
            installed = ", ".join(sorted(available.keys())) if available else "none"
            return (
                f"⚠️ Local agent `{agent}` is not available on this machine.\n"
                f"Installed agents: {installed}"
            )

        blocked_by = self._delegation_safety_block_reason(task)
        if blocked_by:
            log.warning(
                f"[{session_id}] Blocked delegated task by safety policy "
                f"(agent={agent}, pattern={blocked_by})"
            )
            return (
                "🛑 Delegation blocked by local safety policy.\n"
                "Reason: potentially destructive task pattern detected.\n"
                f"Matched rule: `{blocked_by}`\n"
                "If this is intentional, set `LOCAL_AGENT_SAFETY_MODE=off` and restart."
            )

        progress_interval = max(10, int(self.config.local_agent_progress_interval_sec))

        target_workspace: Path
        if workspace_dir is None:
            target_workspace = await asyncio.to_thread(self._create_task_workspace, task)
        else:
            target_workspace = Path(workspace_dir).expanduser().resolve()
            target_workspace.mkdir(parents=True, exist_ok=True)
        workspace_label = self._workspace_rel_label(target_workspace)

        if progress_cb:
            try:
                await progress_cb(
                    (
                        f"🧠 {agent} started. I'll post summarized progress about every "
                        f"{progress_interval}s.\n"
                        f"📁 Task workspace: `{workspace_label}`"
                    )
                )
            except Exception:
                pass

        before = await asyncio.to_thread(self._snapshot_workspace_state, target_workspace)
        result = await self._invoke_local_agent_streaming(
            agent=agent,
            task=task,
            workspace=target_workspace,
            progress_cb=progress_cb,
        )
        after = await asyncio.to_thread(self._snapshot_workspace_state, target_workspace)

        summary = self._compact_external_agent_summary(str(result.get("summary") or ""))
        delta_summary = self._summarize_workspace_delta(before, after)
        stderr_excerpt = self._compact_external_agent_summary(
            self._strip_ansi(str(result.get("stderr") or ""))
        )

        lines = [f"🤖 Delegated to `{agent}`"]
        lines.append(f"📁 Task workspace: `{workspace_label}`")
        if result.get("ok"):
            lines.append(f"✅ Finished in {float(result.get('elapsed', 0.0)):.1f}s")
        elif result.get("timed_out"):
            lines.append(
                f"⚠️ Timed out after {int(self.config.local_agent_timeout_sec)}s"
            )
        else:
            lines.append(
                f"⚠️ `{agent}` exited with code {int(result.get('exit_code', 1))}"
            )

        if summary:
            lines.append("")
            lines.append(summary)

        if include_workspace_delta:
            lines.append("")
            lines.append(delta_summary)

        if not result.get("ok") and stderr_excerpt:
            lines.append("")
            lines.append(f"stderr: {stderr_excerpt[:700]}")

        log.info(
            f"[{session_id}] Local agent {agent} finished "
            f"(ok={result.get('ok')}, exit={result.get('exit_code')}, "
            f"elapsed={float(result.get('elapsed', 0.0)):.1f}s)"
        )
        return "\n".join(lines).strip()
