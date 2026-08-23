"""Telegram inline approval, risk confirmation, and run-control actions."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..artifacts import ArtifactError, accept_artifact, reject_artifact
from ..jobs import JobStateError
from ..markdown import _escape_html


class BotApprovalsMixin:
    _SECOND_CONFIRM_PATTERNS = (
        r"\b(delete|remove|destroy|drop|truncate|wipe|reset|clean)\b",
        r"\b(push|publish|release|deploy|production|open\s+(?:a\s+)?pr)\b",
        r"\b(token|credential|password|secret|api[_ -]?key|permission)\b",
        r"\b(outside|external|system|home directory|/etc/)\b",
    )

    @staticmethod
    def _inline_plan_keyboard(*, second_confirmation: bool = False) -> InlineKeyboardMarkup:
        if second_confirmation:
            return InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("⚠️ Confirm high-risk run", callback_data="lc:plan:confirm-risk"),
                    ],
                    [InlineKeyboardButton("Deny", callback_data="lc:plan:deny")],
                ]
            )
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Approve", callback_data="lc:plan:approve"),
                    InlineKeyboardButton("Edit scope", callback_data="lc:plan:edit"),
                    InlineKeyboardButton("Deny", callback_data="lc:plan:deny"),
                ],
                [InlineKeyboardButton("Cancel active run", callback_data="lc:run:cancel")],
            ]
        )

    @staticmethod
    def _inline_voice_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Use transcription", callback_data="lc:voice:approve"),
                    InlineKeyboardButton("Discard", callback_data="lc:voice:deny"),
                ]
            ]
        )

    @staticmethod
    def _inline_result_keyboard(failed_lanes: list[str] | None = None) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton("View diff", callback_data="lc:run:diff"),
                InlineKeyboardButton("Accept result", callback_data="lc:run:accept"),
            ],
            [
                InlineKeyboardButton("Reject result", callback_data="lc:run:reject"),
                InlineKeyboardButton("Cancel", callback_data="lc:run:cancel"),
            ],
        ]
        if failed_lanes:
            safe_label = re.sub(r"[^a-z0-9_-]", "", failed_lanes[0].lower())[:24]
            if safe_label:
                rows.insert(
                    1,
                    [
                        InlineKeyboardButton(
                            f"Retry {safe_label}",
                            callback_data=f"lc:run:retry:{safe_label}",
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
                        commands.append(command)
        combined = " ".join([goal, *paths, *commands]).lower()
        high_risk = any(re.search(pattern, combined) for pattern in self._SECOND_CONFIRM_PATTERNS)
        worker_count = max(1, len(contracts))
        item["review"] = {
            "risk_level": "high" if high_risk else "medium",
            "changed_paths": list(dict.fromkeys(paths))[:20],
            "proposed_commands": list(dict.fromkeys(commands))[:20],
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
                ", ".join(f"<code>{_escape_html(str(command))}</code>" for command in commands[:6])
                if commands
                else "none declared by acceptance contracts"
            ),
            f"Estimated duration: <code>{estimate.get('min', '?')}–{estimate.get('max', '?')} min</code>",
            f"Estimated cost: <code>{_escape_html(str(review.get('estimated_cost', 'unknown')))}</code>",
        ]
        if review.get("second_confirmation_required"):
            lines.append("⚠️ Publishing, credentials, destructive language, or external scope triggered a second confirmation.")
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
        current = asyncio.current_task()
        if current:
            self._active_run_tasks_by_session[session_id] = current
        try:
            await self._execute_pending_multi_plan(proxy, session_id)
        except asyncio.CancelledError:
            await self._reply_logged(proxy, "Canceled active run and its delegated process tree.")
        finally:
            if self._active_run_tasks_by_session.get(session_id) is current:
                self._active_run_tasks_by_session.pop(session_id, None)

    async def handle_run_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query or not update.effective_user or not update.effective_chat:
            return
        if not self.is_allowed(update.effective_user.id):
            await query.answer("Not authorized", show_alert=True)
            return
        await query.answer()
        session_id = self._session_id_from_update(update)
        action = str(query.data or "")
        proxy = self._callback_proxy(update)

        if action == "lc:voice:approve":
            pending = self._pending_voice_goal_by_session.pop(session_id, None)
            if not pending or float(pending.get("expires_at", 0)) < time.time():
                await self._reply_logged(proxy, "Voice transcription expired; send it again.")
                return
            await self._process_user_message(proxy, context, str(pending.get("text") or ""))
            return
        if action == "lc:voice:deny":
            self._pending_voice_goal_by_session.pop(session_id, None)
            await self._reply_logged(proxy, "Discarded voice transcription. Nothing was executed.")
            return

        if action.startswith("lc:plan:"):
            pending = self._get_pending_multi_plan(session_id)
            if not pending:
                await self._reply_logged(proxy, "No pending plan; create a new `/agent multi` request.")
                return
            if action == "lc:plan:edit":
                await self._reply_logged(
                    proxy,
                    "Reply with <code>/agent multi edit &lt;scope changes&gt;</code>. The current plan will not run.",
                    parse_mode=ParseMode.HTML,
                )
                return
            if action == "lc:plan:deny":
                self._clear_pending_multi_plan(session_id)
                await self._reply_logged(proxy, "Denied pending plan. Nothing was executed.")
                return
            review = pending.get("review") if isinstance(pending.get("review"), dict) else {}
            if action == "lc:plan:approve" and review.get("second_confirmation_required"):
                review["second_confirmation_prompted"] = True
                pending["review"] = review
                await self._reply_logged(
                    proxy,
                    "⚠️ <b>Second confirmation required.</b> Review the high-risk scope once more.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=self._inline_plan_keyboard(second_confirmation=True),
                )
                return
            if action == "lc:plan:confirm-risk":
                if not review.get("second_confirmation_prompted"):
                    await self._reply_logged(proxy, "First review and approve the high-risk plan.")
                    return
                review["second_confirmed"] = True
                pending["review"] = review
            await self._execute_approved_plan_action(update, context, session_id)
            return

        if action == "lc:run:cancel":
            run_id = self._active_run_ids_by_session.get(session_id) or self._last_run_ids_by_session.get(session_id)
            if run_id:
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

        if action.startswith("lc:run:retry:"):
            run_id = self._last_run_ids_by_session.get(session_id)
            label = action.rsplit(":", 1)[-1]
            if not run_id:
                await self._reply_logged(proxy, "No previous run is available for retry.")
                return
            try:
                job = await asyncio.to_thread(self.jobs.retry_lane, run_id, label)
            except JobStateError as exc:
                await self._reply_logged(proxy, f"Retry refused: {_escape_html(str(exc))}")
                return
            await self._reply_logged(proxy, f"Queued bounded retry for `{label}` in `{job['run_id']}`.")
            return

        if action == "lc:run:diff":
            await self._send_last_run_diff(proxy, session_id)
            return
        if action == "lc:run:accept":
            await self._accept_last_run_result(proxy, session_id)
            return
        if action == "lc:run:reject":
            await self._reject_last_run_result(proxy, session_id)
            return

        await self._reply_logged(proxy, "Unknown or expired LightClaw action.")

    async def _send_last_run_diff(self, update, session_id: str) -> None:
        receipt_value = self._last_run_receipts_by_session.get(session_id)
        if not receipt_value:
            await self._reply_logged(update, "No completed run receipt is available.")
            return
        try:
            receipt = json.loads(Path(receipt_value).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            await self._reply_logged(update, "The local run receipt is unavailable.")
            return
        changes = receipt.get("file_changes") if isinstance(receipt.get("file_changes"), list) else []
        artifacts = receipt.get("artifacts") if isinstance(receipt.get("artifacts"), list) else []
        patch_path = next(
            (Path(str(path)) for path in artifacts if str(path).endswith("changes.patch")),
            None,
        )
        if patch_path and patch_path.is_file() and update.message:
            try:
                with patch_path.open("rb") as handle:
                    await update.message.reply_document(
                        document=handle,
                        filename=f"{receipt.get('run_id', 'lightclaw')}.patch",
                        caption="Private review patch — nothing has been pushed.",
                    )
                return
            except Exception:
                pass
        lines = [f"Diff summary: {receipt.get('diff_summary', 'not available')}"]
        for item in changes[:40]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('change', 'changed')}: `{item.get('path', '')}`")
        lines.append(f"Private receipt: `{receipt_value}`")
        await self._send_response(None, update, "\n".join(lines))

    async def _accept_last_run_result(self, update, session_id: str) -> None:
        run_id = self._last_run_ids_by_session.get(session_id)
        if not run_id:
            await self._reply_logged(update, "No completed run is available to accept.")
            return
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

    async def _reject_last_run_result(self, update, session_id: str) -> None:
        run_id = self._last_run_ids_by_session.get(session_id)
        if not run_id:
            await self._reply_logged(update, "No completed run is available to reject.")
            return
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
