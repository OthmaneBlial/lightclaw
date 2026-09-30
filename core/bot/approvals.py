"""Telegram inline approval, risk confirmation, and run-control actions."""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from pathlib import Path
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..artifacts import ArtifactError, accept_artifact, reject_artifact
from ..constants import TELEGRAM_BOT_API_MAX_FILE_BYTES
from ..jobs import JobStateError
from ..markdown import _escape_html
from ..receipts import read_receipt

MAX_REVIEWED_COMMANDS = 6


class BotApprovalsMixin:
    _SECOND_CONFIRM_PATTERNS = (
        r"\b(delete|remove|destroy|drop|truncate|wipe|reset|clean)\b",
        r"\b(push|publish|release|deploy|production|open\s+(?:a\s+)?pr)\b",
        r"\b(token|credential|password|secret|api[_ -]?key|permission)\b",
        r"\b(outside|external|system|home directory|/etc/)\b",
    )

    @staticmethod
    def _inline_plan_keyboard(
        approval_id: str,
        *,
        second_confirmation: bool = False,
        approval_blocked: bool = False,
    ) -> InlineKeyboardMarkup:
        if approval_blocked:
            return InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "Edit plan", callback_data=f"lc:plan:edit:{approval_id}"
                    ),
                    InlineKeyboardButton("Deny", callback_data=f"lc:plan:deny:{approval_id}"),
                ]]
            )
        if second_confirmation:
            return InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⚠️ Confirm high-risk run",
                            callback_data=f"lc:plan:confirm-risk:{approval_id}",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "Deny", callback_data=f"lc:plan:deny:{approval_id}"
                        )
                    ],
                ]
            )
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Approve", callback_data=f"lc:plan:approve:{approval_id}"
                    ),
                    InlineKeyboardButton(
                        "Edit scope", callback_data=f"lc:plan:edit:{approval_id}"
                    ),
                    InlineKeyboardButton(
                        "Deny", callback_data=f"lc:plan:deny:{approval_id}"
                    ),
                ],
            ]
        )

    @staticmethod
    def _inline_voice_keyboard(approval_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Use transcription",
                        callback_data=f"lc:voice:approve:{approval_id}",
                    ),
                    InlineKeyboardButton(
                        "Discard", callback_data=f"lc:voice:deny:{approval_id}"
                    ),
                ]
            ]
        )

    @staticmethod
    def _run_action_token(run_id: str) -> str:
        return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _inline_cancel_keyboard(run_id: str) -> InlineKeyboardMarkup:
        token = BotApprovalsMixin._run_action_token(run_id)
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("Cancel run", callback_data=f"lc:run:cancel:{token}")]]
        )

    @staticmethod
    def _inline_result_keyboard(
        run_id: str,
        failed_lanes: list[str] | None = None,
    ) -> InlineKeyboardMarkup:
        run_token = BotApprovalsMixin._run_action_token(run_id)
        rows = [
            [
                InlineKeyboardButton(
                    "View diff", callback_data=f"lc:run:diff:{run_token}"
                ),
                InlineKeyboardButton(
                    "Accept result", callback_data=f"lc:run:accept:{run_token}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "Reject result", callback_data=f"lc:run:reject:{run_token}"
                ),
            ],
        ]
        if failed_lanes:
            safe_label = re.sub(r"[^a-z0-9_-]", "", failed_lanes[0].lower())[:32]
            if safe_label:
                rows.insert(
                    1,
                    [
                        InlineKeyboardButton(
                            f"Retry {safe_label}",
                            callback_data=f"lc:run:retry:{run_token}:{safe_label}",
                        )
                    ],
                )
        return InlineKeyboardMarkup(rows)

    def _decorate_pending_plan(self, payload: dict[str, object]) -> dict[str, object]:
        item = dict(payload)
        goal = str(item.get("goal") or "")
        plan = item.get("plan_payload") if isinstance(item.get("plan_payload"), dict) else {}
        contracts = plan.get("workers") if isinstance(plan.get("workers"), list) else []
        paths: list[str] = []
        commands: list[str] = []
        for contract in contracts:
            if not isinstance(contract, dict):
                continue
            owned = contract.get("owned_paths")
            if isinstance(owned, list):
                paths.extend(str(path).strip() for path in owned if str(path).strip())
            checks = contract.get("acceptance_checks")
            if isinstance(checks, list):
                for check in checks:
                    if not isinstance(check, dict):
                        continue
                    command = str(check.get("command") or "").strip()
                    if command:
                        label = str(contract.get("label") or "worker").strip()
                        cwd = str(check.get("cwd") or "").strip()
                        location = f" (cwd: {cwd})" if cwd else ""
                        commands.append(f"{label}{location}: {command}")
        combined = " ".join([goal, *paths, *commands]).lower()
        high_risk = any(re.search(pattern, combined) for pattern in self._SECOND_CONFIRM_PATTERNS)
        worker_count = max(1, len(contracts))
        item["review"] = {
            "risk_level": "high" if high_risk else "medium",
            "changed_paths": list(dict.fromkeys(paths))[:20],
            "proposed_commands": commands[:20],
            "approval_blocked": len(commands) > MAX_REVIEWED_COMMANDS,
            "estimated_minutes": {"min": worker_count * 2, "max": worker_count * 15},
            "estimated_cost": "not available from local CLI before execution",
            "second_confirmation_required": high_risk,
            "second_confirmation_prompted": False,
            "second_confirmed": False,
        }
        return item

    @staticmethod
    def _render_plan_review(payload: dict[str, object]) -> str:
        review = payload.get("review") if isinstance(payload.get("review"), dict) else {}
        paths = review.get("changed_paths") if isinstance(review.get("changed_paths"), list) else []
        commands = (
            review.get("proposed_commands")
            if isinstance(review.get("proposed_commands"), list)
            else []
        )
        estimate = (
            review.get("estimated_minutes")
            if isinstance(review.get("estimated_minutes"), dict)
            else {}
        )
        lines = [
            "<b>Approval review</b>",
            f"Risk: <code>{_escape_html(str(review.get('risk_level', 'medium')))}</code>",
            "Changed paths: "
            + (
                ", ".join(f"<code>{_escape_html(str(path))}</code>" for path in paths[:8])
                if paths
                else "not declared; approval should be denied or scope edited"
            ),
            "Proposed commands: "
            + (
                ", ".join(
                    f"<code>{_escape_html(str(command))}</code>"
                    for command in commands[:MAX_REVIEWED_COMMANDS]
                )
                if commands
                else "none declared by acceptance contracts"
            ),
            f"Estimated duration: <code>{estimate.get('min', '?')}–{estimate.get('max', '?')} min</code>",
            f"Estimated cost: <code>{_escape_html(str(review.get('estimated_cost', 'unknown')))}</code>",
        ]
        if commands:
            lines.append(
                "⚠️ Acceptance commands run on the host; LightClaw does not sandbox them."
            )
        if review.get("second_confirmation_required"):
            lines.append("⚠️ Publishing, credentials, destructive language, or external scope triggered a second confirmation.")
        if review.get("approval_blocked"):
            lines.append(
                f"⛔ Blocked: first {MAX_REVIEWED_COMMANDS} commands shown; edit to review all."
            )
        return "\n".join(lines)

    @staticmethod
    def _callback_proxy(update: Update):
        query = update.callback_query
        return SimpleNamespace(
            effective_user=update.effective_user,
            effective_chat=update.effective_chat,
            message=query.message if query else update.effective_message,
        )

    async def _execute_approved_plan_action(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        session_id: str,
    ) -> None:
        proxy = self._callback_proxy(update)
        try:
            await self._execute_pending_multi_plan(proxy, session_id)
        except asyncio.CancelledError:
            await self._reply_logged(proxy, "Canceled active run and its delegated process tree.")

    async def handle_run_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query or not update.effective_user or not update.effective_chat:
            return
        if not self.is_update_allowed(update):
            await query.answer("Not authorized", show_alert=True)
            return
        await query.answer()
        session_id = self._session_id_from_update(update)
        action = str(query.data or "")
        proxy = self._callback_proxy(update)

        if action.startswith("lc:voice:"):
            parts = action.split(":")
            if len(parts) != 4 or parts[2] not in {"approve", "deny"}:
                await self._reply_logged(proxy, "Unknown or expired voice action.")
                return
            decision, callback_id = parts[2], parts[3]
            pending = self._pending_voice_goal_by_session.get(session_id)
            if (
                not pending
                or not re.fullmatch(r"[0-9a-f]{16}", callback_id)
                or not secrets.compare_digest(
                    str(pending.get("approval_id") or ""), callback_id
                )
            ):
                await self._reply_logged(
                    proxy, "This voice approval is stale; review the latest transcription."
                )
                return
            self._pending_voice_goal_by_session.pop(session_id, None)
            if self._pending_confirmation_expired(pending):
                await self._reply_logged(proxy, "Voice transcription expired; send it again.")
                return
            if decision == "approve":
                await self._process_user_message(
                    proxy, context, str(pending.get("text") or "")
                )
            else:
                await self._reply_logged(
                    proxy, "Discarded voice transcription. Nothing was executed."
                )
            return

        if action.startswith("lc:plan:"):
            parts = action.split(":")
            if len(parts) != 4 or parts[2] not in {
                "approve",
                "edit",
                "deny",
                "confirm-risk",
            }:
                await self._reply_logged(proxy, "Unknown or expired plan action.")
                return
            decision, callback_id = parts[2], parts[3]
            pending = self._get_pending_multi_plan(session_id)
            if not pending:
                await self._reply_logged(proxy, "No pending plan; create a new `/agent multi` request.")
                return
            if (
                not re.fullmatch(r"[0-9a-f]{16}", callback_id)
                or not secrets.compare_digest(
                    str(pending.get("approval_id") or ""), callback_id
                )
            ):
                await self._reply_logged(
                    proxy, "This plan approval is stale; review the latest plan."
                )
                return
            if decision == "edit":
                await self._reply_logged(
                    proxy,
                    "Reply with <code>/agent multi edit &lt;scope changes&gt;</code>. The current plan will not run.",
                    parse_mode=ParseMode.HTML,
                )
                return
            if decision == "deny":
                self._clear_pending_multi_plan(session_id)
                await self._reply_logged(proxy, "Denied pending plan. Nothing was executed.")
                return
            review = pending.get("review") if isinstance(pending.get("review"), dict) else {}
            if decision in {"approve", "confirm-risk"} and review.get("approval_blocked"):
                await self._reply_logged(proxy, "Approval blocked; edit the plan to expose all commands.")
                return
            if decision == "approve" and review.get("second_confirmation_required"):
                review["second_confirmation_prompted"] = True
                pending["review"] = review
                await self._reply_logged(
                    proxy,
                    "⚠️ <b>Second confirmation required.</b> Review the high-risk scope once more.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=self._inline_plan_keyboard(
                        callback_id,
                        second_confirmation=True,
                    ),
                )
                return
            if decision == "confirm-risk":
                if not review.get("second_confirmation_prompted"):
                    await self._reply_logged(proxy, "First review and approve the high-risk plan.")
                    return
                review["second_confirmed"] = True
                pending["review"] = review
            elif decision != "approve":
                await self._reply_logged(proxy, "Unknown or expired plan action.")
                return
            await self._execute_approved_plan_action(update, context, session_id)
            return

        if action.startswith("lc:run:cancel:"):
            parts = action.split(":")
            run_id = self._active_run_ids_by_session.get(session_id)
            if (
                len(parts) != 4
                or not run_id
                or not re.fullmatch(r"[0-9a-f]{16}", parts[3])
                or not secrets.compare_digest(parts[3], self._run_action_token(run_id))
            ):
                await self._reply_logged(
                    proxy, "This cancel button belongs to a run that is no longer active."
                )
                return
            try:
                await asyncio.to_thread(self.jobs.request_cancel, run_id)
            except JobStateError:
                pass
            task = self._active_run_tasks_by_session.get(session_id)
            if task and task is not asyncio.current_task():
                task.cancel()
                await self._reply_logged(proxy, "Cancellation requested; stopping the delegated process tree.")
            else:
                await self._reply_logged(proxy, "No active run to cancel.")
            return

        if action.startswith("lc:run:"):
            parts = action.split(":")
            run_id = self._last_run_ids_by_session.get(session_id)
            if (
                not run_id
                or len(parts) not in {4, 5}
                or parts[2] not in {"diff", "accept", "reject", "retry"}
                or (parts[2] == "retry") != (len(parts) == 5)
                or not re.fullmatch(r"[0-9a-f]{16}", parts[3])
                or not secrets.compare_digest(
                    parts[3], self._run_action_token(run_id)
                )
            ):
                await self._reply_logged(
                    proxy, "This result action is stale; review the latest run."
                )
                return
            decision = parts[2]
            label = parts[4] if decision == "retry" else ""
            if decision == "retry" and not re.fullmatch(r"[a-z0-9_-]{1,24}", label):
                await self._reply_logged(proxy, "Unknown or expired retry action.")
                return
            if decision == "diff":
                await self._send_last_run_diff(proxy, session_id, run_id)
                return
            if run_id in self._result_actions_in_flight:
                await self._reply_logged(
                    proxy, "A result action for this run is already in progress."
                )
                return
            self._result_actions_in_flight.add(run_id)
            try:
                if decision == "retry":
                    try:
                        job = await asyncio.to_thread(
                            self.jobs.retry_lane, run_id, label
                        )
                    except JobStateError as exc:
                        await self._reply_logged(
                            proxy, f"Retry refused: {_escape_html(str(exc))}"
                        )
                        return
                    await self._reply_logged(
                        proxy,
                        f"Queued bounded retry for `{label}` in `{job['run_id']}`.",
                    )
                elif decision == "accept":
                    await self._accept_last_run_result(proxy, session_id, run_id)
                else:
                    await self._reject_last_run_result(proxy, session_id, run_id)
            finally:
                self._result_actions_in_flight.discard(run_id)
            return

        await self._reply_logged(proxy, "Unknown or expired LightClaw action.")

    async def _send_last_run_diff(self, update, session_id: str, run_id: str) -> None:
        receipt_value = self._last_run_receipts_by_session.get(session_id)
        if not receipt_value:
            await self._reply_logged(update, "No completed run receipt is available.")
            return
        try:
            receipt = read_receipt(receipt_value)
        except ValueError:
            await self._reply_logged(update, "The local run receipt is unavailable.")
            return
        if str(receipt.get("run_id") or "") != run_id:
            await self._reply_logged(update, "This result button no longer matches its run.")
            return
        changes = receipt.get("file_changes") if isinstance(receipt.get("file_changes"), list) else []
        artifacts = receipt.get("artifacts") if isinstance(receipt.get("artifacts"), list) else []
        patch_path = next(
            (Path(str(path)) for path in artifacts if str(path).endswith("changes.patch")),
            None,
        )
        raw_summary = str(receipt.get("diff_summary") or "not available").strip()
        summary = next(
            (
                line.strip()
                for line in reversed(raw_summary.splitlines())
                if " changed" in line
            ),
            raw_summary.splitlines()[0] if raw_summary else "not available",
        )
        if len(summary) > 240:
            summary = summary[:237].rstrip() + "..."
        review_lines = [f"Diff summary: {summary}"]
        changed_files = [item for item in changes if isinstance(item, dict)]
        if changed_files:
            review_lines.append("Changed files:")
            for item in changed_files[:12]:
                status = re.sub(r"\s+", " ", str(item.get("change") or "changed"))
                path = re.sub(r"\s+", " ", str(item.get("path") or ""))
                if len(path) > 180:
                    path = path[:177].rstrip() + "..."
                review_lines.append(f"- {status[:24]}: {path}")
            if len(changed_files) > 12:
                review_lines.append(f"- and {len(changed_files) - 12} more files")
        patch_size: int | None = None
        if patch_path and update.message:
            try:
                if patch_path.is_file() and not patch_path.is_symlink():
                    patch_size = patch_path.stat().st_size
            except OSError:
                pass

        if patch_path and patch_size is not None and patch_size > TELEGRAM_BOT_API_MAX_FILE_BYTES:
            review_lines.append(
                f"Full patch is too large to attach through Telegram; review it locally: `{patch_path}`"
            )
            await self._reply_logged(update, "\n".join(review_lines))
            return

        if patch_path and patch_size is not None and update.message:
            review_lines.append("Full patch attached below; nothing has been accepted or pushed.")
            await self._reply_logged(update, "\n".join(review_lines))
            try:
                with patch_path.open("rb") as handle:
                    await update.message.reply_document(
                        document=InputFile(
                            handle,
                            filename=f"{receipt.get('run_id', 'lightclaw')}.patch",
                            read_file_handle=False,
                        ),
                        caption="Private review patch — nothing has been accepted or pushed.",
                    )
            except Exception:
                await self._reply_logged(
                    update,
                    "Could not attach the full patch. The summary above is available; review the local receipt on the host.",
                )
            return
        review_lines.append(f"Private receipt on host: {receipt_value}")
        await self._reply_logged(update, "\n".join(review_lines))

    async def _accept_last_run_result(self, update, session_id: str, run_id: str) -> None:
        try:
            job = await asyncio.to_thread(self.jobs.get_job, run_id)
            if job["status"] != "succeeded":
                raise JobStateError(f"run is {job['status']}, not succeeded")
            workspace = self._last_run_workspaces_by_session.get(session_id) or str(job["workspace"])
            artifact = await asyncio.to_thread(accept_artifact, workspace, run_id)
            job = await asyncio.to_thread(self.jobs.accept, run_id)
        except (ArtifactError, JobStateError) as exc:
            await self._reply_logged(update, f"Accept refused: {_escape_html(str(exc))}")
            return
        await self._reply_logged(
            update,
            f"Accepted local result `{job['run_id']}` at commit `{artifact['commit']}`. Nothing was pushed or published.",
        )

    async def _reject_last_run_result(self, update, session_id: str, run_id: str) -> None:
        try:
            job = await asyncio.to_thread(self.jobs.get_job, run_id)
            if job["status"] not in {"succeeded", "failed"}:
                raise JobStateError(f"run is {job['status']}, not finished")
            workspace = self._last_run_workspaces_by_session.get(session_id) or str(job["workspace"])
            await asyncio.to_thread(reject_artifact, workspace, run_id)
            job = await asyncio.to_thread(self.jobs.reject, run_id)
        except (ArtifactError, JobStateError) as exc:
            await self._reply_logged(update, f"Reject refused: {_escape_html(str(exc))}")
            return
        await self._reply_logged(
            update,
            f"Rejected `{job['run_id']}`. Workspace files were preserved for review; nothing was published.",
        )
