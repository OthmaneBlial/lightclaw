"""Delegated command execution, progress ingestion, and result formatting."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import signal
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from ...artifacts import ArtifactError, create_patch_bundle
from ...logging_setup import log
from ...receipts import write_receipt
from ...security import delegated_process_env, redact_text
from .agents import DelegationAgentsMixin
from .streams import BoundedStreamCapture
from .workspace import (
    await_task_completion,
    await_thread_completion,
    create_subprocess_at,
    stop_process_tree,
)


class DelegationExecutionMixin:
    _strip_ansi = staticmethod(DelegationAgentsMixin._strip_ansi)
    _parse_codex_exec_output = DelegationAgentsMixin._parse_codex_exec_output
    _parse_claude_cli_output = DelegationAgentsMixin._parse_claude_cli_output

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

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

    def _build_local_agent_command(
        self,
        agent: str,
        workspace: Path,
        prompt: str,
        stream_output: bool,
        capability_profile: str | None = None,
    ) -> tuple[list[str], str | None]:
        profile = (
            capability_profile
            or getattr(self.config, "local_agent_capability_profile", "workspace-write")
        ).strip().lower()
        if profile not in {"observe", "workspace-write", "trusted-command"}:
            profile = "workspace-write"
        run_input: str | None = None
        if agent == "codex":
            cmd = [
                "codex",
                "exec",
                "--json",
                "--ephemeral",
                "--skip-git-repo-check",
                "--color",
                "never",
            ]
            if profile == "trusted-command":
                cmd.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                sandbox = "read-only" if profile == "observe" else "workspace-write"
                cmd.extend(["--sandbox", sandbox])
            cmd.extend(["-C", ".", "-"])
            run_input = prompt
            return cmd, run_input

        if agent == "claude":
            cmd = [
                "claude",
                "-p",
                "--no-chrome",
                "--no-session-persistence",
            ]
            if profile == "trusted-command":
                cmd.append("--dangerously-skip-permissions")
            else:
                permission_mode = "plan" if profile == "observe" else "acceptEdits"
                cmd.extend(["--permission-mode", permission_mode])
            cmd.append("-")
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
        cleaned = re.sub(r"\s+", " ", redact_text(text).strip())
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
            "command_records": [],
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
                records = state.get("command_records")
                if isinstance(records, list) and len(records) < 200:
                    output = self._short_progress_text(
                        str(item.get("aggregated_output") or item.get("output") or ""),
                        max_chars=500,
                    )
                    records.append(
                        {
                            "command": cmd or "codex command",
                            "exit_code": exit_code,
                            "summary": output or ("passed" if exit_code == 0 else "failed"),
                        }
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
                        records = state.get("command_records")
                        if isinstance(records, list) and len(records) < 200:
                            records.append(
                                {
                                    "command": f"claude tool: {tool_name}",
                                    "exit_code": None,
                                    "summary": "tool invocation reported; exit status unavailable",
                                }
                            )
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
                    records = state.get("command_records")
                    if isinstance(records, list) and len(records) < 200:
                        records.append(
                            {
                                "command": f"claude tool: {tool_name}",
                                "exit_code": None,
                                "summary": "tool invocation reported; exit status unavailable",
                            }
                        )
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
        capability_profile: str | None = None,
        job_run_id: str | None = None,
    ) -> dict:
        workspace = (workspace or Path(self.config.workspace_path).resolve()).resolve()
        timeout_sec = max(1, int(self.config.local_agent_timeout_sec))
        progress_interval = max(10, int(self.config.local_agent_progress_interval_sec))
        prompt = self._build_delegation_prompt(task, workspace=workspace)
        env = delegated_process_env(
            extra={"LIGHTCLAW_DELEGATED_AGENT": agent, "CI": "1"}
        )

        cmd, run_input = self._build_local_agent_command(
            agent=agent,
            workspace=workspace,
            prompt=prompt,
            stream_output=True,
            capability_profile=capability_profile,
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
        async def terminate_before_streaming(*, force: bool = False) -> None:
            async def drain(stream):
                while await stream.read(65536):
                    pass

            await asyncio.gather(
                stop_process_tree(proc, force=force), drain(proc.stdout), drain(proc.stderr)
            )

        creation_task = asyncio.create_task(
            create_subprocess_at(
                workspace, (), *cmd,
                stdin=asyncio.subprocess.PIPE if run_input is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        )
        try:
            proc = await await_task_completion(creation_task)
        except asyncio.CancelledError:
            if not creation_task.cancelled() and creation_task.exception() is None:
                proc = creation_task.result()
                await await_task_completion(asyncio.create_task(terminate_before_streaming()))
            raise
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

        process_store = getattr(self, "jobs", None)
        process_group_registered = False
        if job_run_id and process_store is not None:
            registration_task = asyncio.create_task(
                asyncio.to_thread(
                    process_store.register_process_group, job_run_id, proc.pid
                )
            )
            try:
                await asyncio.shield(registration_task)
            except asyncio.CancelledError:
                async def abort_registration() -> None:
                    await terminate_before_streaming()
                    try:
                        await registration_task
                    except Exception:
                        return
                    try:
                        await await_thread_completion(
                            process_store.unregister_process_group,
                            job_run_id,
                            proc.pid,
                        )
                    except Exception:
                        log.exception(
                            "Canceled delegated process group could not be unregistered"
                        )

                cleanup_task = asyncio.create_task(abort_registration())
                await await_task_completion(cleanup_task)
                raise
            except Exception as e:
                await await_task_completion(
                    asyncio.create_task(terminate_before_streaming(force=True))
                )
                return {
                    "ok": False,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": f"could not register delegated process group: {e}",
                    "summary": "",
                    "elapsed": 0.0,
                    "timed_out": False,
                }
            else:
                process_group_registered = True

        state = self._new_progress_state()
        stdout_capture = BoundedStreamCapture("stdout")
        stderr_capture = BoundedStreamCapture("stderr")
        heartbeat_stop = asyncio.Event()

        async def emit_progress(text: str):
            if not progress_cb:
                return
            try:
                await progress_cb(text)
            except Exception:
                # Progress updates are best-effort and must not fail delegation.
                pass

        parse_warning_emitted: set[str] = set()

        async def read_stream(stream, collector: BoundedStreamCapture):
            if stream is None:
                return
            async for line in collector.read_lines(stream):
                try:
                    self._ingest_progress_event(agent, line, state, collector.name)
                except Exception as e:
                    # Progress parsing is best-effort; never crash the worker on it.
                    state["errors"] = int(state.get("errors", 0)) + 1
                    state["last_activity"] = self._short_progress_text(
                        f"progress parser warning: {e}",
                        max_chars=220,
                    )
                    if collector.name not in parse_warning_emitted:
                        parse_warning_emitted.add(collector.name)
                        log.warning("Delegation progress event could not be parsed")

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
        io_failed = False
        deadline = time.monotonic() + timeout_sec
        streams_task = asyncio.gather(
            read_stream(proc.stdout, stdout_capture),
            read_stream(proc.stderr, stderr_capture),
            proc.wait(),
        )
        try:
            if run_input is not None and proc.stdin:
                try:
                    proc.stdin.write(run_input.encode("utf-8"))
                    await asyncio.wait_for(
                        proc.stdin.drain(),
                        timeout=max(0, deadline - time.monotonic()),
                    )
                except asyncio.TimeoutError:
                    raise
                except Exception as exc:
                    io_failed = True
                    stderr_capture.append_line(f"Prompt stdin failed ({type(exc).__name__})")
                finally:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
            await asyncio.wait_for(
                streams_task,
                timeout=max(0, deadline - time.monotonic()),
            )
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                proc.kill()
            await proc.wait()
            if not streams_task.done():
                try:
                    await asyncio.wait_for(streams_task, timeout=0.25)
                except Exception:
                    pass
            await asyncio.gather(streams_task, return_exceptions=True)
            stderr_capture.append_line(f"Timed out after {timeout_sec}s")
        except asyncio.CancelledError:
            cleanup_task = asyncio.create_task(stop_process_tree(proc))
            try:
                await await_task_completion(cleanup_task)
            finally:
                if not streams_task.done():
                    streams_task.cancel()
                await await_task_completion(
                    asyncio.gather(streams_task, return_exceptions=True)
                )
            raise
        except Exception as exc:
            io_failed = True
            await stop_process_tree(proc)
            if not streams_task.done():
                streams_task.cancel()
            await asyncio.gather(streams_task, return_exceptions=True)
            stderr_capture.append_line(f"Agent output stream failed: {exc}")
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
            if process_group_registered:
                try:
                    await await_thread_completion(
                        process_store.unregister_process_group,
                        job_run_id,
                        proc.pid,
                    )
                except Exception:
                    log.exception("Delegated process group could not be unregistered")

        elapsed = time.monotonic() - started
        exit_code = 124 if timed_out else int(proc.returncode if proc.returncode is not None else 1)
        if io_failed:
            exit_code = 1
        stdout = redact_text(stdout_capture.text())
        stderr = redact_text(stderr_capture.text())

        if agent == "codex":
            summary = self._parse_codex_exec_output(stdout)
        else:
            summary = self._parse_claude_cli_output(stdout)
        output_truncated = stdout_capture.truncated or stderr_capture.truncated
        if output_truncated:
            summary = (
                "⚠️ Agent output was truncated by LightClaw; review changed files and the run receipt.\n\n"
                f"{summary}"
            ).strip()
        summary = redact_text(summary)

        ok = exit_code == 0
        if summary.strip().lower().startswith("error:"):
            ok = False

        return {
            "ok": ok,
            "exit_code": int(exit_code),
            "stdout": stdout,
            "stderr": stderr,
            "summary": summary,
            "output_truncated": output_truncated,
            "elapsed": elapsed,
            "timed_out": timed_out,
            "commands": list(state.get("command_records", [])),
        }

    async def _run_local_agent_task(
        self,
        session_id: str,
        agent: str,
        task: str,
        progress_cb: Callable[[str], Awaitable[None]] | None = None,
        include_workspace_delta: bool = True,
        workspace_dir: Path | str | None = None,
        capability_profile: str | None = None,
        emit_receipt: bool = True,
        evidence_sink: dict[str, object] | None = None,
        manage_job: bool = True,
        initialize_artifact: bool = True,
        process_owner_run_id: str | None = None,
    ) -> str:
        if getattr(self, "_shutting_down", False):
            return "⏹ LightClaw is shutting down; no agent work was started."
        if not manage_job:
            return await self._run_local_agent_task_impl(
                session_id=session_id,
                agent=agent,
                task=task,
                progress_cb=progress_cb,
                include_workspace_delta=include_workspace_delta,
                workspace_dir=workspace_dir,
                capability_profile=capability_profile,
                emit_receipt=emit_receipt,
                evidence_sink=evidence_sink,
                manage_job=False,
                initialize_artifact=initialize_artifact,
                process_owner_run_id=process_owner_run_id,
            )

        locks = getattr(self, "_session_run_locks", None)
        if locks is None:
            locks = self._session_run_locks = {}
        lock = locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            return "⏳ An agent run is already active in this chat. Wait for it or cancel it before starting another."
        try:
            async with lock:
                current = asyncio.current_task()
                active = getattr(self, "_active_run_tasks_by_session", None)
                if active is None:
                    active = self._active_run_tasks_by_session = {}
                if current:
                    active[session_id] = current
                try:
                    return await self._run_local_agent_task_impl(
                        session_id=session_id,
                        agent=agent,
                        task=task,
                        progress_cb=progress_cb,
                        include_workspace_delta=include_workspace_delta,
                        workspace_dir=workspace_dir,
                        capability_profile=capability_profile,
                        emit_receipt=emit_receipt,
                        evidence_sink=evidence_sink,
                        manage_job=True,
                        initialize_artifact=initialize_artifact,
                        process_owner_run_id=process_owner_run_id,
                    )
                finally:
                    if current and active.get(session_id) is current:
                        active.pop(session_id, None)
        finally:
            if locks.get(session_id) is lock and not lock.locked():
                locks.pop(session_id, None)

    async def _run_local_agent_task_impl(
        self,
        session_id: str,
        agent: str,
        task: str,
        progress_cb: Callable[[str], Awaitable[None]] | None = None,
        include_workspace_delta: bool = True,
        workspace_dir: Path | str | None = None,
        capability_profile: str | None = None,
        emit_receipt: bool = True,
        evidence_sink: dict[str, object] | None = None,
        manage_job: bool = True,
        initialize_artifact: bool = True,
        process_owner_run_id: str | None = None,
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
            log.warning("Delegated task blocked by safety policy")
            return (
                "🛑 Delegation blocked by local safety policy.\n"
                "Reason: potentially destructive task pattern detected.\n"
                f"Matched rule: `{blocked_by}`\n"
                "If this is intentional, set `LOCAL_AGENT_SAFETY_MODE=off` and restart."
            )

        progress_interval = max(10, int(self.config.local_agent_progress_interval_sec))
        profile = (
            capability_profile or self.config.local_agent_capability_profile
        ).strip().lower()
        if profile not in {"observe", "workspace-write", "trusted-command"}:
            profile = "workspace-write"

        target_workspace, owns_workspace = await self._resolve_task_workspace(task, workspace_dir)
        workspace_label = self._workspace_rel_label(target_workspace)
        started_at = self._utc_now()
        run_id = f"run-{int(time.time())}-{secrets.token_hex(4)}"
        try:
            checkpoint = await self._prepare_task_workspace_checkpoint(
                target_workspace,
                run_id,
                initialize_artifact=initialize_artifact,
                owns_workspace=owns_workspace,
            )
        except ArtifactError as exc:
            return f"⚠️ Could not create the isolated Git checkpoint: {exc}\n" \
                f"Task workspace preserved for inspection: {workspace_label}"
        for attribute in (
            "_active_run_ids_by_session",
            "_active_run_tasks_by_session",
            "_last_run_ids_by_session",
            "_last_run_receipts_by_session",
        ):
            if not hasattr(self, attribute):
                setattr(self, attribute, {})
        durable_store = getattr(self, "jobs", None) if manage_job else None
        heartbeat_task: asyncio.Task[None] | None = None
        if durable_store is not None:
            durable_plan = [
                {
                    "label": "delegation",
                    "worker": agent,
                    "depends_on": [],
                    "owned_paths": [],
                    "idempotent": False,
                    "resumable": False,
                    "max_attempts": 1,
                }
            ]
            try:
                durable = await await_thread_completion(
                    durable_store.create_job,
                    workspace=target_workspace,
                    session_id=session_id,
                    goal=task,
                    approved_scope=f"LightClaw-owned task workspace: {workspace_label}",
                    risk_level="high" if profile == "trusted-command" else "medium",
                    capability_profile=profile,
                    plan=durable_plan,
                    status="queued",
                    resumable=False,
                    max_retries=0,
                    run_id=run_id,
                )
                claimed = await await_thread_completion(
                    durable_store.claim_next,
                    workspace=target_workspace,
                    worker_pid=os.getpid(),
                    run_id=run_id,
                )
                if not claimed or claimed["run_id"] != durable["run_id"]:
                    return f"⏳ Delegation queued as `{run_id}`; another run is ahead of it or owns this workspace."
                self._active_run_ids_by_session[session_id] = run_id
                current_run_task = asyncio.current_task()
                if current_run_task:
                    self._active_run_tasks_by_session[session_id] = current_run_task
                await await_thread_completion(
                    durable_store.update_lane,
                    run_id,
                    "delegation",
                    "running",
                    increment_attempt=True,
                )
                delegated_task = asyncio.current_task()

                async def durable_heartbeat() -> None:
                    while True:
                        await asyncio.sleep(30)
                        current_job, stop = await self._durable_job_heartbeat(
                            durable_store, run_id, worker_pid=os.getpid()
                        )
                        if stop:
                            return
                        if current_job and current_job["status"] == "cancel_requested" and delegated_task:
                            self._cancel_task_once(delegated_task)
                            return

                heartbeat_task = asyncio.create_task(durable_heartbeat())
            except asyncio.CancelledError:
                await self._cancel_durable_delegation(
                    durable_store,
                    session_id,
                    run_id,
                    target_workspace,
                    owns_workspace=owns_workspace,
                )
                raise
            except Exception as exc:
                return await self._handle_durable_setup_failure(
                    exc,
                    durable_store,
                    session_id,
                    run_id,
                    target_workspace,
                    owns_workspace,
                )

        try:
            if progress_cb:
                try:
                    await progress_cb(
                        (
                            f"🧠 {agent} started. I'll post summarized progress about every "
                            f"{progress_interval}s.\n"
                            f"📁 Task workspace: `{workspace_label}`\n"
                            f"🔐 Capability: `{profile}`"
                        )
                    )
                except Exception:
                    pass
            before = await await_thread_completion(
                self._snapshot_workspace_state, target_workspace
            )
            result = await self._invoke_local_agent_streaming(
                agent=agent,
                task=task,
                workspace=target_workspace,
                progress_cb=progress_cb,
                capability_profile=profile,
                job_run_id=(process_owner_run_id or run_id)
                if durable_store is not None or process_owner_run_id
                else None,
            )
        except asyncio.CancelledError:
            if durable_store is not None:
                await self._cancel_durable_delegation(
                    durable_store,
                    session_id,
                    run_id,
                    target_workspace,
                    owns_workspace=False,
                )
            if self._active_run_ids_by_session.get(session_id) == run_id:
                self._active_run_ids_by_session.pop(session_id, None)
            raise
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
        after = await self._await_durable_run_phase(
            await_thread_completion(
                self._snapshot_workspace_state, target_workspace
            ),
            durable_store,
            session_id,
            run_id,
        )

        summary = self._compact_external_agent_summary(str(result.get("summary") or ""))
        delta_summary = self._summarize_workspace_delta(before, after)
        stderr_excerpt = self._compact_external_agent_summary(
            self._strip_ansi(str(result.get("stderr") or ""))
        )
        file_changes = await self._await_durable_run_phase(
            await_thread_completion(
                self._workspace_file_changes, target_workspace, before, after
            ),
            durable_store,
            session_id,
            run_id,
        )
        command_name = f"{agent} delegated invocation; prompt passed through stdin"
        reported_commands = result.get("commands")
        commands = (
            [dict(item) for item in reported_commands if isinstance(item, dict)]
            if isinstance(reported_commands, list)
            else []
        )
        commands.insert(
            0,
            {
                "command": command_name,
                "exit_code": int(result.get("exit_code", 1)),
                "summary": summary or stderr_excerpt or "no summary reported",
            },
        )
        check = {
            "name": "delegated process exit status",
            "passed": bool(result.get("ok")),
            "evidence": (
                f"exit {int(result.get('exit_code', 1))}; "
                f"elapsed {float(result.get('elapsed', 0.0)):.3f}s"
            ),
        }
        receipt_output = self._receipt_output_dir(run_id)
        artifact_bundle: dict[str, object] | None = None
        if initialize_artifact:
            try:
                artifact_bundle = await self._await_durable_run_phase(
                    await_thread_completion(
                        create_patch_bundle,
                        target_workspace,
                        receipt_output,
                        run_id=run_id,
                    ),
                    durable_store,
                    session_id,
                    run_id,
                )
            except ArtifactError as exc:
                artifact_bundle = {"error": str(exc), "diff_stat": "patch generation failed"}
        artifact_ok = bool(artifact_bundle is None or not artifact_bundle.get("error"))
        checks = [check]
        if initialize_artifact:
            checks.append(
                {
                    "name": "review patch generated",
                    "passed": artifact_ok,
                    "evidence": (
                        "private Git patch and manifest recorded"
                        if artifact_ok
                        else str(artifact_bundle.get("error") or "patch generation failed")
                    ),
                }
            )
        run_ok = bool(result.get("ok")) and artifact_ok
        durable_error = ""
        if durable_store is not None:
            artifact_error = (
                str(artifact_bundle.get("error") or "") if artifact_bundle else ""
            )
            durable_error = await self._await_durable_run_phase(
                self._finalize_durable_delegation(
                    durable_store, run_id, result, artifact_error
                ),
                durable_store,
                session_id,
                run_id,
            )
            checks.append(
                {
                    "name": "durable job finalization",
                    "passed": not durable_error,
                    "evidence": durable_error or "terminal job state recorded",
                }
            )
        run_ok = run_ok and not durable_error
        artifact_paths = [
            item["path"] for item in file_changes if item.get("change") != "deleted"
        ]
        if artifact_bundle:
            for key in ("patch", "manifest"):
                value = artifact_bundle.get(key)
                if value:
                    artifact_paths.append(str(value))

        receipt = {
            "run_id": run_id,
            "original_goal": task,
            "approved_scope": f"LightClaw-owned task workspace: {workspace_label}",
            "risk_level": "high" if profile == "trusted-command" else "medium",
            "capability_profile": profile,
            "plan": [
                {
                    "label": "delegation",
                    "worker": agent,
                    "model": "local CLI account routing",
                    "depends_on": [],
                    "task": task,
                }
            ],
            "started_at": started_at,
            "finished_at": self._utc_now(),
            "duration_seconds": round(float(result.get("elapsed", 0.0)), 3),
            "usage": {
                "provider": agent,
                "tokens": None,
                "estimated_cost_usd": None,
                "note": "local CLI did not expose bounded usage to LightClaw",
            },
            "commands": commands,
            "file_changes": file_changes,
            "diff_summary": (
                str(artifact_bundle.get("diff_stat") or "").strip()
                if artifact_bundle
                else self._compact_diff_summary(file_changes)
            ),
            "checks": checks,
            "handoffs": [],
            "artifacts": artifact_paths,
            "failures": (
                []
                if run_ok
                else [
                    f"Durable job finalization failed: {durable_error}"
                    if durable_error
                    else str(artifact_bundle.get("error") or "patch generation failed")
                    if not artifact_ok and artifact_bundle
                    else stderr_excerpt or "delegated process failed"
                ]
            ),
            "retries": 0,
            "disposition": "ready_for_review" if run_ok else "failed",
            "checkpoint": checkpoint,
            "undo": f"lightclaw undo {target_workspace.name} --apply",
            "workspace": target_workspace.as_posix(),
            "session": session_id,
        }
        receipt_paths: tuple[Path, Path] | None = None
        if emit_receipt:
            receipt_json, receipt_markdown, safe_receipt = await self._await_durable_run_phase(
                await_thread_completion(write_receipt, receipt, receipt_output),
                durable_store,
                session_id,
                run_id,
            )
            receipt_paths = (receipt_json, receipt_markdown)
            receipt = safe_receipt
            self._last_run_ids_by_session[session_id] = run_id
            self._last_run_receipts_by_session[session_id] = receipt_json.as_posix()
        if evidence_sink is not None:
            evidence_sink.clear()
            evidence_sink.update(receipt)
            if receipt_paths:
                evidence_sink["receipt_json"] = receipt_paths[0].as_posix()
                evidence_sink["receipt_markdown"] = receipt_paths[1].as_posix()

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
        if durable_error:
            lines.append("⚠️ Local job history could not record a terminal state; receipt is marked failed.")

        if summary:
            lines.append("")
            lines.append(summary)

        if include_workspace_delta:
            lines.append("")
            lines.append(delta_summary)

        if not result.get("ok") and stderr_excerpt:
            lines.append("")
            lines.append(f"stderr: {stderr_excerpt[:700]}")

        if receipt_paths:
            lines.append("")
            lines.append(f"🧾 Receipt: `{receipt_paths[1].as_posix()}`")

        log.info("Local agent run finished")
        if self._active_run_ids_by_session.get(session_id) == run_id:
            self._active_run_ids_by_session.pop(session_id, None)
        if self._active_run_tasks_by_session.get(session_id) is asyncio.current_task():
            self._active_run_tasks_by_session.pop(session_id, None)
        return "\n".join(lines).strip()
