"""Telegram inline approval, risk confirmation, and run-control actions."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
from pathlib import Path
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ..artifacts import (
    ArtifactError,
    accept_artifact,
    read_review_manifest,
    reject_artifact,
)
from ..constants import TELEGRAM_BOT_API_MAX_FILE_BYTES
from ..fs import open_regular_file_at
from ..jobs import JobStateError
from ..logging_setup import log
from ..markdown import _escape_html
from ..receipts import read_receipt
from ..security import redact_text
from .delegation.workspace import await_thread_completion

MAX_REVIEWED_COMMANDS = 6
MAX_DIFF_PREVIEW_BYTES = 32 * 1024
_MAX_HISTORY_PAGE = 99999
_MAX_HISTORY_SNAPSHOT = 9223372036854775807
_UNSAFE_REVIEW_CONTROLS = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200b\u200e\u200f\u202a-\u202e\u2060\u2066-\u206f\ufeff\ud800-\udfff]"
)


def _history_number(value: str, maximum: int) -> int | None:
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,18})", value):
        return None
    number = int(value)
    return number if number <= maximum else None


class BotApprovalsMixin:
    _SECOND_CONFIRM_PATTERNS = (
        r"\b(delete|remove|destroy|drop|truncate|wipe|reset|clean|overwrite|purge|erase|revoke|restore|rm|rmdir|rmtree|unlink|shred|wipefs|mkfs|dd|mv|cp|tee|sed|perl|chmod|chown)\b",
        r"\bgit\s+(?:-C\s+\S+\s+)?checkout\b[^\n]*?(?:--\s|-[fp]\b)",
        r"\b(push|publish|release|deploy|production|merge|open\s+(?:a\s+)?pr)\b",
        r"\b(tokens?|credentials?|passwords?|secrets?|permissions?|(?:api|access|private)[_ -]?keys?)\b",
        r"\b(outside|external|system|home directory|sudo|doas)\b|/etc/",
        r"\b(?:os\.(?:replace|rename)|shutil\.(?:move|copy|copy2|copyfile|copytree)|(?:pathlib\.)?path(?:\([^)]*\))?\.(?:replace|rename))\s*\(",
        r"\b(curl|wget|ssh|scp|sftp|nc|netcat|ftp|telnet)\b|https?://",
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
    def _mobile_diff_preview(patch: str) -> str:
        excerpt: list[str] = []
        in_hunk = False
        for line in patch.splitlines():
            if line.startswith("diff --git ") and in_hunk:
                break
            elif line.startswith("@@"):
                if in_hunk:
                    break
                in_hunk = True
                excerpt.append(BotApprovalsMixin._visible_review_text(line))
            elif in_hunk and line[:1] in {" ", "+", "-"}:
                safe_line = BotApprovalsMixin._visible_review_text(line)
                if len(safe_line) > 140:
                    safe_line = safe_line[:137] + "..."
                excerpt.append(safe_line)
                if len(excerpt) >= 9:
                    break
        if not excerpt:
            return "No text hunk; full patch attached."
        return "\n".join(excerpt)

    @staticmethod
    def _visible_review_text(value: object) -> str:
        return _UNSAFE_REVIEW_CONTROLS.sub("�", str(value if value is not None else ""))

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
    def _inline_trusted_keyboard(approval_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("Approve host run", callback_data=f"lc:trusted:approve:{approval_id}"),
            InlineKeyboardButton("Discard", callback_data=f"lc:trusted:deny:{approval_id}"),
        ]])

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
        risk_text: list[str] = []
        for contract in contracts:
            if not isinstance(contract, dict):
                continue
            for field in ("role", "responsibilities", "expected_inputs", "expected_outputs"):
                value = contract.get(field)
                if isinstance(value, str):
                    risk_text.append(value)
                elif isinstance(value, list):
                    risk_text.extend(str(text).strip() for text in value if str(text).strip())
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
        combined = " ".join([goal, *risk_text, *paths, *commands]).lower()
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
        estimate = review.get("estimated_minutes") if isinstance(review.get("estimated_minutes"), dict) else {}
        lines = [
            "<b>Approval review</b>",
            f"Risk: <code>{_escape_html(BotApprovalsMixin._visible_review_text(review.get('risk_level', 'medium')))}</code>",
            "Changed paths: "
            + (
                ", ".join(f"<code>{_escape_html(BotApprovalsMixin._visible_review_text(path))}</code>" for path in paths[:8])
                if paths
                else "not declared; approval should be denied or scope edited"
            ),
            "Proposed commands: "
            + (
                ", ".join(
                    f"<code>{_escape_html(BotApprovalsMixin._visible_review_text(command))}</code>"
                    for command in commands[:MAX_REVIEWED_COMMANDS]
                )
                if commands
                else "none declared by acceptance contracts"
            ),
            f"Estimated duration: <code>{estimate.get('min', '?')}–{estimate.get('max', '?')} min</code>",
            f"Estimated cost: <code>{_escape_html(BotApprovalsMixin._visible_review_text(review.get('estimated_cost', 'unknown')))}</code>",
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

    async def _require_complete_plan_review(self, update, pending: dict[str, object]) -> bool:
        if pending.get("review_delivered") is True:
            return True
        await self._reply_logged(
            update, "Wait for the complete plan review before approving. No run was started."
        )
        return False

    async def _prompt_second_confirmation(self, update, pending: dict[str, object]) -> None:
        if not await self._require_complete_plan_review(update, pending):
            return
        review = pending.get("review") if isinstance(pending.get("review"), dict) else {}
        await self._reply_logged(
            update,
            "⚠️ <b>Second confirmation required.</b> Review the high-risk scope once more.",
            parse_mode=ParseMode.HTML,
            reply_markup=self._inline_plan_keyboard(
                str(pending.get("approval_id") or ""), second_confirmation=True
            ),
        )
        review["second_confirmation_prompted"] = True
        pending["review"] = review

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

    async def _cancel_queued_history_run(
        self,
        update: Update,
        session_id: str,
        page: int,
        snapshot_rowid: int,
        token: str,
    ) -> None:
        proxy = self._callback_proxy(update)
        if update.effective_chat.type != "private":
            await self._reply_logged(
                proxy, "Queued-run cancellation is available in private chat only."
            )
            return
        jobs, _has_more, _snapshot = await self._load_recent_runs_page(
            session_id, page, snapshot_rowid
        )
        job = next(
            (
                item
                for item in jobs
                if secrets.compare_digest(
                    token, self._run_action_token(str(item.get("run_id") or ""))
                )
            ),
            None,
        )
        if not job or job.get("status") != "queued":
            await self._reply_logged(
                proxy, "This queued run is no longer available to cancel."
            )
            return
        run_id = str(job.get("run_id") or "")
        try:
            canceled = await asyncio.to_thread(self.jobs.request_cancel, run_id)
        except JobStateError:
            await self._reply_logged(
                proxy, "This run changed state. Refresh /agent runs and try again."
            )
            return
        except Exception:
            log.exception("Could not cancel queued run %s", run_id)
            await self._reply_logged(
                proxy, "Could not save the cancellation. Check job storage and retry."
            )
            return
        if canceled["status"] == "cancel_requested":
            active_ids = getattr(self, "_active_run_ids_by_session", {})
            task = getattr(self, "_active_run_tasks_by_session", {}).get(session_id)
            if (
                active_ids.get(session_id) == run_id
                and task
                and task is not asyncio.current_task()
                and not task.done()
            ):
                self._cancel_task_once(task)
        jobs, has_more, snapshot_rowid = await self._load_recent_runs_page(
            session_id, page, snapshot_rowid
        )
        text = redact_text(
            self._render_recent_runs(jobs, page),
            getattr(getattr(self, "config", None), "__dict__", {}),
        )
        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=self._recent_runs_keyboard(
                jobs,
                page=page,
                has_more=has_more,
                snapshot_rowid=snapshot_rowid,
                allow_cancel=True,
            ),
        )
        self._log_bot_message(session_id, self._strip_html_for_log(text))

    async def handle_run_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query or not update.effective_user or not update.effective_chat:
            return
        if not self.is_update_allowed(update):
            await query.answer("Not authorized", show_alert=True)
            return
        await query.answer()
        session_id = await self._session_scope_from_update(update)
        action = str(query.data or "")
        proxy = self._callback_proxy(update)

        if action.startswith("lc:trusted:"):
            parts = action.split(":")
            if (
                len(parts) != 4
                or parts[2] not in {"approve", "deny"}
                or not re.fullmatch(r"[0-9a-f]{16}", parts[3])
            ):
                await self._reply_logged(proxy, "Unknown or expired trusted-run action.")
                return
            decision = "confirm" if parts[2] == "approve" else "discard"
            await self.cmd_agent(proxy, SimpleNamespace(args=["trusted", decision, parts[3]]))
            return

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
            if not self._is_pending_requester(update, pending):
                await self._reply_logged(
                    proxy,
                    "Only the user who sent this voice request can approve or discard it.",
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

        if action.startswith("lc:history:"):
            parts = action.split(":")
            if parts[2] == "page" and len(parts) in {4, 5}:
                page = _history_number(parts[3], _MAX_HISTORY_PAGE)
                snapshot_text = parts[4] if len(parts) == 5 else None
                snapshot_rowid = (
                    _history_number(snapshot_text, _MAX_HISTORY_SNAPSHOT)
                    if snapshot_text is not None
                    else None
                )
                if page is None or (
                    snapshot_text is not None and snapshot_rowid is None
                ):
                    await self._reply_logged(proxy, "This history page is invalid.")
                    return
                jobs, has_more, snapshot_rowid = await self._load_recent_runs_page(
                    session_id, page, snapshot_rowid
                )
                text = redact_text(
                    self._render_recent_runs(jobs, page),
                    getattr(getattr(self, "config", None), "__dict__", {}),
                )
                try:
                    await query.edit_message_text(
                        text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=self._recent_runs_keyboard(
                            jobs,
                            page=page,
                            has_more=has_more,
                            snapshot_rowid=snapshot_rowid,
                            allow_cancel=(
                                getattr(update.effective_chat, "type", None) == "private"
                            ),
                        ),
                    )
                except BadRequest as exc:
                    if "message is not modified" not in str(exc).lower():
                        raise
                    return
                self._log_bot_message(
                    session_id, self._strip_html_for_log(text)
                )
                return

            if len(parts) == 6 and parts[2] == "cancel":
                page = _history_number(parts[3], _MAX_HISTORY_PAGE)
                snapshot_rowid = _history_number(
                    parts[4], _MAX_HISTORY_SNAPSHOT
                )
                token = parts[5]
                if (
                    page is None
                    or snapshot_rowid is None
                    or not re.fullmatch(r"[0-9a-f]{16}", token)
                ):
                    await self._reply_logged(
                        proxy, "This queued-run button is invalid or expired."
                    )
                    return
                await self._cancel_queued_history_run(
                    update, session_id, page, snapshot_rowid, token
                )
                return

            page = 0
            snapshot_rowid = None
            if len(parts) == 6 and parts[2] == "diff":
                page = _history_number(parts[3], _MAX_HISTORY_PAGE)
                snapshot_rowid = _history_number(
                    parts[4], _MAX_HISTORY_SNAPSHOT
                )
                token = parts[5]
                if page is None or snapshot_rowid is None:
                    await self._reply_logged(
                        proxy, "This run is no longer on the history page for this chat."
                    )
                    return
            elif len(parts) == 5 and parts[2] == "diff":
                page = _history_number(parts[3], _MAX_HISTORY_PAGE)
                if page is None:
                    await self._reply_logged(
                        proxy, "This run is no longer on the history page for this chat."
                    )
                    return
                token = parts[4]
            elif len(parts) == 4 and parts[2] == "diff":
                token = parts[3]
            else:
                token = ""
            if (
                not re.fullmatch(r"[0-9a-f]{16}", token)
            ):
                await self._reply_logged(
                    proxy, "This run is no longer on the history page for this chat."
                )
                return
            jobs, _has_more, _snapshot_rowid = await self._load_recent_runs_page(
                session_id, page, snapshot_rowid
            )
            job = next(
                (
                    item
                    for item in jobs
                    if secrets.compare_digest(
                        token, self._run_action_token(str(item.get("run_id") or ""))
                    )
                ),
                None,
            )
            if not job:
                await self._reply_logged(
                    proxy, "This run is no longer on the history page for this chat."
                )
                return
            run_id = str(job.get("run_id") or "")
            if job.get("status") not in {"succeeded", "failed", "accepted", "rejected"}:
                await self._reply_logged(proxy, "The run diff is available after the run finishes.")
                return
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
                await self._reply_logged(proxy, "The local run receipt is unavailable.")
                return
            receipt_path = (
                Path(".lightclaw-meta") / "receipts" / run_id / "receipt.json"
            )
            await self._send_last_run_diff(
                proxy, session_id, run_id, receipt_value=receipt_path
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
            if not self._is_pending_requester(update, pending):
                await self._reply_logged(
                    proxy, "Only the requester can act on this plan."
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
            if decision in {"approve", "confirm-risk"} and not await self._require_complete_plan_review(proxy, pending):
                return
            if decision in {"approve", "confirm-risk"} and review.get("approval_blocked"):
                await self._reply_logged(proxy, "Approval blocked; edit the plan to expose all commands.")
                return
            if decision == "approve" and review.get("second_confirmation_required"):
                await self._prompt_second_confirmation(proxy, pending)
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
            if (
                update.effective_chat.type != "private"
                and not self._is_pending_requester(
                    update,
                    {
                        "user_id": getattr(
                            self, "_active_run_requesters_by_session", {}
                        ).get(session_id)
                    },
                )
            ):
                await self._reply_logged(proxy, "Only the requester can cancel this run.")
                return
            task = self._active_run_tasks_by_session.get(session_id)
            stopping_locally = bool(
                task and task is not asyncio.current_task() and not task.done()
            )
            if stopping_locally:
                self._cancel_task_once(task)
            try:
                await asyncio.to_thread(self.jobs.request_cancel, run_id)
            except JobStateError:
                pass
            except Exception:
                log.exception("Could not persist cancellation request for run %s", run_id)
                message = (
                    "Local stop started, but the job state could not be saved. "
                    "Check the run status and retry if it remains active."
                    if stopping_locally
                    else "Could not save the cancellation. Check job storage and try again."
                )
                await self._reply_logged(proxy, message)
                return
            if stopping_locally:
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
            if decision == "retry" and not re.fullmatch(r"[a-z0-9_-]{1,32}", label):
                await self._reply_logged(proxy, "Unknown or expired retry action.")
                return
            if decision == "diff":
                await self._send_last_run_diff(proxy, session_id, run_id)
                return
            if (
                update.effective_chat.type != "private"
                and not self._is_pending_requester(
                    update,
                    {
                        "user_id": getattr(
                            self, "_last_run_requesters_by_session", {}
                        ).get(session_id)
                    },
                )
            ):
                await self._reply_logged(proxy, "Only the requester can change this run result.")
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

    async def _send_last_run_diff(
        self,
        update,
        session_id: str,
        run_id: str,
        *,
        receipt_value: str | Path | None = None,
    ) -> None:
        receipt_value = receipt_value or self._last_run_receipts_by_session.get(session_id)
        if not receipt_value:
            await self._reply_logged(update, "No completed run receipt is available.")
            return
        try:
            workspace = Path(self.config.workspace_path).expanduser().resolve()
            relative_receipt = Path(receipt_value)
            if relative_receipt.is_absolute():
                relative_receipt = relative_receipt.relative_to(workspace)
            receipt = await await_thread_completion(
                read_receipt, relative_receipt, root=workspace
            )
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
            for item in changed_files[:8]:
                status = re.sub(r"\s+", " ", self._visible_review_text(item.get("change") or "changed"))
                path = re.sub(r"\s+", " ", self._visible_review_text(item.get("path") or ""))
                if len(path) > 120:
                    path = path[:117].rstrip() + "..."
                review_lines.append(f"- {status[:24]}: {path}")
            if len(changed_files) > 8:
                review_lines.append(f"- and {len(changed_files) - 8} more files")
        patch_handle = None
        patch_size: int | None = None
        if patch_path and update.message:
            patch_fd: int | None = None

            def open_patch():
                workspace = Path(self.config.workspace_path).expanduser().resolve()
                relative_patch = patch_path.absolute().relative_to(workspace)
                opened_fd, opened_stat = open_regular_file_at(workspace, relative_patch)
                opened_patch_fds.append(opened_fd)
                return opened_fd, opened_stat

            opened_patch_fds: list[int] = []
            try:
                patch_fd, patch_stat = await await_thread_completion(open_patch)
                patch_handle = os.fdopen(patch_fd, "rb")
                patch_fd = None
                opened_patch_fds.clear()
                patch_size = patch_stat.st_size
            except asyncio.CancelledError:
                if opened_patch_fds:
                    os.close(opened_patch_fds.pop())
                raise
            except (OSError, RuntimeError, ValueError):
                if patch_fd is not None:
                    os.close(patch_fd)

        if patch_path and patch_size is not None and patch_size > TELEGRAM_BOT_API_MAX_FILE_BYTES:
            if patch_handle:
                patch_handle.close()
            review_lines.append(
                f"Full patch is too large to attach through Telegram; review it locally: `{patch_path}`"
            )
            await self._reply_logged(update, "\n".join(review_lines))
            return

        if patch_path and update.message:
            if patch_handle is None or patch_size is None:
                review_lines.extend(["", "Patch is unavailable or could not be opened safely."])
                await self._reply_logged(update, "\n".join(review_lines))
                return
            try:
                if patch_size <= MAX_DIFF_PREVIEW_BYTES:
                    try:
                        patch_bytes = await await_thread_completion(
                            patch_handle.read, MAX_DIFF_PREVIEW_BYTES + 1
                        )
                        if len(patch_bytes) > MAX_DIFF_PREVIEW_BYTES:
                            raise ValueError("patch grew beyond preview limit")
                        patch = patch_bytes.decode("utf-8")
                        review_lines.extend(
                            ["", "Patch preview (first text hunk):", self._mobile_diff_preview(patch)]
                        )
                    except (OSError, UnicodeError, ValueError):
                        review_lines.extend(["", "Inline preview unavailable; full patch attached."])
                    finally:
                        await await_thread_completion(patch_handle.seek, 0)
                else:
                    review_lines.extend(
                        ["", "Patch exceeds the inline preview limit; full patch attached."]
                    )
                review_lines.append("Full patch attached below; nothing has been accepted or pushed.")
                await self._reply_logged(update, "\n".join(review_lines))
                try:
                    with patch_handle:
                        await update.message.reply_document(
                            document=InputFile(
                                patch_handle,
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
            finally:
                if not patch_handle.closed:
                    patch_handle.close()
            return
        review_lines.append(f"Private receipt on host: {receipt_value}")
        await self._reply_logged(update, "\n".join(review_lines))

    async def _accept_last_run_result(self, update, session_id: str, run_id: str) -> None:
        try:
            job = await asyncio.to_thread(self.jobs.get_job, run_id)
            if job["status"] != "succeeded":
                raise JobStateError(f"run is {job['status']}, not succeeded")
            workspace = str(job["workspace"])
            reviewed_manifest = await asyncio.to_thread(
                read_review_manifest,
                self._last_run_receipts_by_session.get(session_id) or "",
                run_id=run_id,
            )
            artifact = await asyncio.to_thread(
                accept_artifact,
                workspace,
                run_id,
                reviewed_manifest=reviewed_manifest,
            )
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
            workspace = str(job["workspace"])
            await asyncio.to_thread(reject_artifact, workspace, run_id)
            job = await asyncio.to_thread(self.jobs.reject, run_id)
        except (ArtifactError, JobStateError) as exc:
            await self._reply_logged(update, f"Reject refused: {_escape_html(str(exc))}")
            return
        await self._reply_logged(
            update,
            f"Rejected `{job['run_id']}`. Workspace files were preserved for review; nothing was published.",
        )
