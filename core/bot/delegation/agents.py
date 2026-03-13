"""Agent selection, argument parsing, and status rendering helpers."""

from __future__ import annotations

import os
import re
import shutil

from ...markdown import _escape_html


class DelegationAgentsMixin:
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
            "<code>/agent multi --agent ... --depends-on &lt;label=dep1,dep2&gt; &lt;goal&gt;</code> - explicit worker DAG\n"
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

    @staticmethod
    def _is_valid_multi_label(label: str) -> bool:
        return bool(re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", (label or "").strip().lower()))

    def _parse_multi_dependency_spec(
        self,
        raw_spec: str,
    ) -> tuple[tuple[str, list[str]] | None, str]:
        value = (raw_spec or "").strip()
        if "=" not in value:
            return (
                None,
                (
                    "Invalid dependency spec: "
                    f"<code>{_escape_html(value)}</code>.\n"
                    "Use format <code>label=dep1,dep2</code>, for example "
                    "<code>master=research,review</code>."
                ),
            )
        label_raw, deps_raw = value.split("=", 1)
        label = label_raw.strip().lower()
        if not self._is_valid_multi_label(label):
            return (
                None,
                (
                    f"Invalid dependency label <code>{_escape_html(label)}</code>.\n"
                    "Allowed: lowercase letters, digits, <code>_</code>, <code>-</code>, "
                    "must start with a letter."
                ),
            )
        deps: list[str] = []
        seen: set[str] = set()
        for item in deps_raw.split(","):
            dep = item.strip().lower()
            if not dep:
                continue
            if not self._is_valid_multi_label(dep):
                return (
                    None,
                    (
                        f"Invalid dependency target <code>{_escape_html(dep)}</code>.\n"
                        "Allowed: lowercase letters, digits, <code>_</code>, <code>-</code>, "
                        "must start with a letter."
                    ),
                )
            if dep not in seen:
                seen.add(dep)
                deps.append(dep)
        if not deps:
            return (
                None,
                (
                    "Dependency spec must include at least one dependency target.\n"
                    "Example: <code>master=research,review</code>."
                ),
            )
        if label in seen:
            return (
                None,
                f"Dependency spec for <code>{_escape_html(label)}</code> cannot include itself.",
            )
        return (label, deps), ""

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
        dependency_specs: dict[str, list[str]] = {}
        i = 0
        while i < len(tokens):
            token = (tokens[i] or "").strip()
            option = token
            inline_value = ""
            if token.startswith("--agent="):
                option = "--agent"
                inline_value = token.split("=", 1)[1].strip()
            elif token.startswith("--depends-on="):
                option = "--depends-on"
                inline_value = token.split("=", 1)[1].strip()

            if option not in {"--agent", "-a", "--depends-on", "-d"}:
                break
            if inline_value:
                raw_spec = inline_value
                i += 1
            else:
                if i + 1 >= len(tokens):
                    if option in {"--depends-on", "-d"}:
                        return {}, "Missing value after <code>--depends-on</code>."
                    return {}, "Missing value after <code>--agent</code>."
                raw_spec = (tokens[i + 1] or "").strip()
                i += 2

            if option in {"--agent", "-a"}:
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
                if not self._is_valid_multi_label(label):
                    return (
                        {},
                        (
                            f"Invalid label <code>{_escape_html(label)}</code>.\n"
                            "Allowed: lowercase letters, digits, <code>_</code>, <code>-</code>, "
                            "must start with a letter."
                        ),
                    )
                specs.append((label, agent))
                continue

            parsed_dep, dep_error = self._parse_multi_dependency_spec(raw_spec)
            if dep_error:
                return {}, dep_error
            if not parsed_dep:
                continue
            label, deps = parsed_dep
            merged = dependency_specs.setdefault(label, [])
            for dep in deps:
                if dep not in merged:
                    merged.append(dep)

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

        for owner, deps in dependency_specs.items():
            if owner not in seen:
                return (
                    {},
                    (
                        f"Dependency spec references unknown worker <code>{_escape_html(owner)}</code>.\n"
                        "Declare it first with <code>--agent label=agent</code>."
                    ),
                )
            unknown = [dep for dep in deps if dep not in seen]
            if unknown:
                return (
                    {},
                    (
                        f"Dependency spec for <code>{_escape_html(owner)}</code> references unknown worker(s): "
                        f"<code>{_escape_html(', '.join(unknown))}</code>."
                    ),
                )

        return {
            "action": "proposal",
            "goal": goal,
            "explicit_specs": specs,
            "explicit_dependency_specs": dependency_specs,
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
        lines.append(
            "<b>Multi repair attempts:</b> "
            + _escape_html(str(self.config.local_agent_multi_repair_attempts))
        )
        lines.append("")
        lines.append(self._agent_usage_text())
        return "\n".join(lines)
