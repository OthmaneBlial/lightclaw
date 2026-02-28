"""Delegated command execution, progress ingestion, and result formatting."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import os
from pathlib import Path
import re
import subprocess
import time

from ...logging_setup import log


class DelegationExecutionMixin:
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
