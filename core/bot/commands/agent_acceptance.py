"""/agent command handlers and multi-agent execution orchestration."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import signal
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ...fs import FileTooLargeError, read_json_object
from ...logging_setup import log
from ...security import delegated_process_env
from ..delegation.streams import BoundedStreamCapture

MAX_MULTI_HANDOFF_JSON_BYTES = 1024 * 1024


class CommandsAgentAcceptanceMixin:
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
                path = self._resolve_multi_workspace_path(workspace, normalized)
                if looks_like_path and (path is None or not path.exists()):
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
        *,
        cancel_event: threading.Event | None = None,
        job_run_id: str | None = None,
    ) -> str:
        command = str(check.get("command") or "").strip()
        if not command:
            return "command_succeeds check is missing `command`"

        cwd_rel = self._normalize_multi_contract_path(str(check.get("cwd") or ""))
        cwd = self._resolve_multi_workspace_path(workspace, cwd_rel)
        if cwd is None:
            return f"command_succeeds cwd is outside the workspace: `{cwd_rel}`"
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
        if cancel_event and cancel_event.is_set():
            return f"command canceled: `{command}`"

        async def run_command() -> str:
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(cwd),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=delegated_process_env(extra={"CI": "1"}),
                    start_new_session=os.name == "posix",
                )
            except Exception as e:
                return f"command failed to start `{command}`: {e}"

            stdout_capture = BoundedStreamCapture("stdout")
            stderr_capture = BoundedStreamCapture("stderr")

            async def drain(stream, capture: BoundedStreamCapture) -> None:
                async for _ in capture.read_lines(stream):
                    pass

            stream_tasks = (
                asyncio.create_task(drain(process.stdout, stdout_capture)),
                asyncio.create_task(drain(process.stderr, stderr_capture)),
            )
            wait_task = asyncio.create_task(process.wait())
            stream_completion = asyncio.gather(*stream_tasks, wait_task)

            async def wait_for_cancel() -> None:
                while cancel_event and not cancel_event.is_set():
                    await asyncio.sleep(0.05)

            cancel_task = (
                asyncio.create_task(wait_for_cancel()) if cancel_event else None
            )

            async def stop_process_group() -> None:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    await asyncio.sleep(0.2)
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif process.returncode is None:
                    process.kill()
                await wait_task

            process_store = getattr(self, "jobs", None)
            group_registered = False
            if job_run_id and process_store is not None and os.name == "posix":
                try:
                    await asyncio.to_thread(
                        process_store.register_process_group,
                        job_run_id,
                        process.pid,
                    )
                except Exception as e:
                    await stop_process_group()
                    try:
                        await asyncio.to_thread(
                            process_store.unregister_process_group,
                            job_run_id,
                            process.pid,
                        )
                    except Exception:
                        log.exception(
                            "Failed acceptance process registration left stale state"
                        )
                    for task in stream_tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*stream_tasks, return_exceptions=True)
                    return f"could not register acceptance process group: {e}"
                group_registered = True

            try:
                tasks = [stream_completion]
                if cancel_task:
                    tasks.append(cancel_task)
                done, _ = await asyncio.wait(
                    tasks,
                    timeout=timeout_sec,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                canceled = bool(
                    cancel_task in done and cancel_event and cancel_event.is_set()
                )
                timed_out = not canceled and stream_completion not in done
                stream_error = None
                if not canceled and not timed_out:
                    try:
                        await stream_completion
                    except Exception as e:
                        stream_error = e

                if canceled or timed_out or stream_error:
                    await stop_process_group()
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*stream_tasks), timeout=1
                        )
                    except Exception:
                        for task in stream_tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*stream_tasks, return_exceptions=True)
                    if canceled:
                        return f"command canceled: `{command}`"
                    if stream_error:
                        return f"command output failed `{command}`: {stream_error}"
                    return f"command timed out after {timeout_sec}s: `{command}`"

                if process.returncode == 0:
                    return ""

                output_parts = [stderr_capture.text(), stdout_capture.text()]
                output = "\n".join(part.strip() for part in output_parts if part.strip())
                if stdout_capture.truncated or stderr_capture.truncated:
                    output = f"[acceptance command output truncated]\n{output}"
                output_preview = (
                    self._short_progress_text(output, max_chars=220) if output else ""
                )
                location = f" in `{cwd_rel}`" if cwd_rel else ""
                detail = f": {output_preview}" if output_preview else ""
                return (
                    f"command failed{location} (exit {process.returncode}): `{command}`{detail}"
                )
            finally:
                if cancel_task:
                    cancel_task.cancel()
                    await asyncio.gather(cancel_task, return_exceptions=True)
                if group_registered:
                    try:
                        await asyncio.to_thread(
                            process_store.unregister_process_group,
                            job_run_id,
                            process.pid,
                        )
                    except Exception:
                        log.exception(
                            "Acceptance process group could not be unregistered"
                        )

        try:
            return asyncio.run(run_command())
        except Exception as e:
            return f"command failed to run `{command}`: {e}"

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
        relative_path = self._multi_handoff_json_path(label)
        return self._read_multi_handoff_json(workspace, relative_path)

    @staticmethod
    def _resolve_multi_workspace_path(
        workspace: Path,
        relative_path: str,
    ) -> Path | None:
        try:
            root = workspace.resolve(strict=True)
            path = (root / relative_path).resolve()
            path.relative_to(root)
            return path
        except (OSError, RuntimeError, ValueError):
            return None

    @staticmethod
    def _read_multi_handoff_json(
        workspace: Path,
        display_path: str,
    ) -> tuple[dict[str, Any], str]:
        try:
            payload = read_json_object(display_path, root=workspace, max_bytes=MAX_MULTI_HANDOFF_JSON_BYTES)
            if not payload:
                return {}, f"missing or empty handoff JSON `{display_path}`"
            return payload, ""
        except FileTooLargeError:
            return {}, f"handoff JSON `{display_path}` exceeds 1 MiB limit"
        except (OSError, ValueError) as exc:
            return {}, f"invalid handoff JSON `{display_path}`: {exc}"

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
        *,
        cancel_event: threading.Event | None = None,
        job_run_id: str | None = None,
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
            if cancel_event and cancel_event.is_set():
                return False, ["acceptance checks canceled"], handoff_data
            kind = str(check.get("type") or "").strip().lower()

            if kind == "file_exists":
                rel_path = self._normalize_multi_contract_path(str(check.get("path") or ""))
                path = self._resolve_multi_workspace_path(workspace, rel_path) if rel_path else None
                if path is None or not path.is_file():
                    failures.append(f"missing required file `{rel_path or '(invalid path)'}`")
                continue

            if kind == "handoff_json":
                rel_path = (
                    self._normalize_multi_contract_path(str(check.get("path") or ""))
                    or self._multi_handoff_json_path(label)
                )
                raw, error = self._read_multi_handoff_json(workspace, rel_path)
                if error:
                    failures.append(error)
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
                    matches = [
                        path
                        for item in workspace.glob(pattern)
                        if (path := self._resolve_multi_workspace_path(
                            workspace, item.relative_to(workspace).as_posix()
                        )) is not None
                        and path.is_file()
                    ]
                except Exception as e:
                    failures.append(f"invalid glob pattern `{pattern}`: {e}")
                    continue
                if not matches:
                    failures.append(f"no files matched `{pattern}`")
                continue

            if kind == "command_succeeds":
                command_failure = self._run_multi_acceptance_command(
                    workspace,
                    check,
                    cancel_event=cancel_event,
                    job_run_id=job_run_id,
                )
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
                missing = []
                for reported_path in reported:
                    path = self._resolve_multi_workspace_path(workspace, reported_path)
                    if path is None or not path.exists():
                        missing.append(reported_path)
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
