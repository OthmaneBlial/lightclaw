"""Telegram message chunking/sending and framework error handling."""

from __future__ import annotations

import os
import re
import secrets
import tempfile
import time
from html import unescape
from pathlib import Path

from telegram import InputFile, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, NetworkError, RetryAfter, TimedOut
from telegram.ext import ContextTypes

from ..constants import TELEGRAM_BOT_API_MAX_FILE_BYTES
from ..logging_setup import log
from ..markdown import markdown_to_telegram_html
from ..security import redact_text
from ..workspaces import validate_workspace_root


class BotMessagingMixin:
    def _write_long_response_artifact(self, text: str) -> Path:
        root = validate_workspace_root(self.config.workspace_path)
        output = root / ".lightclaw-meta" / "messages"
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        output.chmod(0o700)
        path = output / f"response-{int(time.time())}-{secrets.token_hex(4)}.md"
        fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=output)
        temp = Path(raw_temp)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(redact_text(text))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            path.chmod(0o600)
        finally:
            temp.unlink(missing_ok=True)
        return path

    @staticmethod
    def _chunk_message(text: str, max_len: int = 3500) -> list[str]:
        """Split Markdown into chunks below Telegram's UTF-16 text limit."""
        if max_len < 1:
            raise ValueError("max_len must be positive")
        if not text or len(text.encode("utf-16-le")) // 2 <= max_len:
            return [text]

        chunks: list[str] = []
        while text:
            units = 0
            safe_cut = 0
            newline_cut = 0
            for index, char in enumerate(text):
                units += 2 if ord(char) > 0xFFFF else 1
                if units > max_len:
                    break
                safe_cut = index + 1
                if char == "\n":
                    newline_cut = safe_cut
            if safe_cut == len(text):
                chunks.append(text)
                break
            split_at = newline_cut or safe_cut
            chunks.append(text[:split_at])
            text = text[split_at:]

        return chunks

    async def _send_response(self, placeholder, update: Update, markdown_response: str):
        """Send bounded Markdown chunks after converting each one to HTML."""
        if len(markdown_response) > 6000 or self._is_large_code_leak(markdown_response):
            artifact = self._write_long_response_artifact(markdown_response)
            summary = (
                "Result is too large for safe inline review. "
                "Attached as a private Markdown artifact."
            )
            if placeholder:
                await self._try_send(placeholder.edit_text, summary)
            if update.message:
                try:
                    artifact_size = artifact.stat().st_size
                except OSError:
                    artifact_size = None
                if artifact_size is None or artifact_size > TELEGRAM_BOT_API_MAX_FILE_BYTES:
                    failure = (
                        "Telegram cannot attach this result because it is too large "
                        "or its size could not be checked. It remains saved locally as "
                        f"`{artifact.name}` under `.lightclaw-meta/messages/`."
                    )
                    if placeholder:
                        await self._try_send(placeholder.edit_text, failure)
                    else:
                        await self._try_send(update.message.reply_text, failure)
                    return
                try:
                    with artifact.open("rb") as handle:
                        await update.message.reply_document(
                            document=InputFile(
                                handle,
                                filename=artifact.name,
                                read_file_handle=False,
                            ),
                            caption="LightClaw result artifact — review before sharing.",
                        )
                    return
                except Exception as exc:
                    log.warning("Could not attach long result artifact: %s", exc)
                    failure = (
                        "Telegram could not attach this oversized result. "
                        f"It remains saved locally as `{artifact.name}` under "
                        "`.lightclaw-meta/messages/`."
                    )
                    if placeholder:
                        await self._try_send(placeholder.edit_text, failure)
                    elif update.message:
                        await self._try_send(update.message.reply_text, failure)
            return

        # First chunk the markdown (before HTML conversion which expands entities)
        markdown_chunks = self._chunk_message(markdown_response, max_len=3000)
        session_id = self._session_id_from_update(update)

        for i, markdown_chunk in enumerate(markdown_chunks):
            self._log_bot_message(session_id, markdown_chunk)
            # Convert each chunk to HTML separately
            html_chunk = markdown_to_telegram_html(markdown_chunk)

            if i == 0 and placeholder:
                # First chunk: edit the placeholder
                sent = await self._try_send(placeholder.edit_text, html_chunk)
                if sent:
                    continue
                # Edit failed — fall through to send as new message

            # Subsequent chunks or fallback: send as new message
            if update.message:
                await self._try_send(update.message.reply_text, html_chunk)

        # If we had multiple chunks, log it
        if len(markdown_chunks) > 1:
            log.info(f"Long response split into {len(markdown_chunks)} messages ({len(markdown_response)} chars)")

    @staticmethod
    def _is_large_code_leak(text: str) -> bool:
        """Detect suspicious large code dumps that should never reach chat."""
        if len(text) < 800:
            return False
        if "```" in text and any(tag in text.lower() for tag in ("```html", "```python", "```javascript", "```css", "```tsx", "```jsx")):
            return True
        indicators = ("<!doctype html", "<html", "tailwind.config", "function(", "className=", "import React", "def main(")
        return sum(1 for i in indicators if i.lower() in text.lower()) >= 2

    # ── Global Telegram Error Handler ────────────────────────

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Handle Telegram framework errors without noisy unstructured tracebacks."""
        err = context.error
        session_id = "unknown"
        if isinstance(update, Update):
            session_id = self._session_id_from_update(update)

        if isinstance(err, Conflict):
            now = time.time()
            # Polling conflicts repeat every few seconds; avoid log spam.
            if now - self._last_telegram_conflict_log_at >= 30:
                self._last_telegram_conflict_log_at = now
                log.warning(
                    f"[{session_id}] Telegram polling conflict: another bot instance is using getUpdates. "
                    "Keep only one `lightclaw run` active for this bot token."
                )
            return
        if isinstance(err, RetryAfter):
            log.warning(f"[{session_id}] Telegram rate limit: retry after {err.retry_after}s")
            return
        if isinstance(err, (TimedOut, NetworkError)):
            log.warning(f"[{session_id}] Telegram network issue: {err}")
            return

        log.exception(f"[{session_id}] Unhandled Telegram error", exc_info=err)

    async def _try_send(self, send_fn, text: str) -> bool:
        """Use plain text only when Telegram rejects the HTML payload."""
        try:
            await send_fn(text, parse_mode=ParseMode.HTML)
            return True
        except BadRequest:
            pass

        # Fallback: strip HTML tags and send as plain text
        try:
            plain = unescape(re.sub(r"<[^>]+>", "", text))
            await send_fn(plain)
            return True
        except BadRequest as e:
            log.error(f"Failed to send message chunk: {e}")
            return False
