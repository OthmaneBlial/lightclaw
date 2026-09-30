"""Telegram message chunking/sending and framework error handling."""

from __future__ import annotations

import re
import secrets
import time
import traceback
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path

from telegram import InputFile, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, NetworkError, RetryAfter, TimedOut
from telegram.ext import ContextTypes

from ..constants import TELEGRAM_BOT_API_MAX_FILE_BYTES
from ..fs import atomic_write_text
from ..logging_setup import log
from ..markdown import markdown_to_telegram_html
from ..security import redact_text
from ..workspaces import WorkspaceSafetyError, ensure_private_workspace_dir


class _TelegramHTMLChunker(HTMLParser):
    def __init__(self, max_len: int):
        super().__init__(convert_charrefs=True)
        self.max_len = max_len
        self.chunks: list[str] = []
        self.parts: list[str] = []
        self.open_tags: list[tuple[str, str, str]] = []
        self.units = 0

    def handle_starttag(self, tag: str, _attrs) -> None:
        opening = self.get_starttag_text() or f"<{tag}>"
        self.parts.append(opening)
        self.open_tags.append((tag, opening, f"</{tag}>"))

    def handle_endtag(self, tag: str) -> None:
        if self.open_tags and self.open_tags[-1][0] == tag:
            self.parts.append(self.open_tags.pop()[2])
        else:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        offset = 0
        while offset < len(data):
            if self.units >= self.max_len:
                self._split()
            available = self.max_len - self.units
            end = offset
            used = 0
            while end < len(data):
                char_units = 2 if ord(data[end]) > 0xFFFF else 1
                if used + char_units > available:
                    break
                used += char_units
                end += 1
            if end == offset:
                if self.units:
                    self._split()
                    continue
                end += 1
                used = 2 if ord(data[offset]) > 0xFFFF else 1
            self.parts.append(escape(data[offset:end], quote=True))
            self.units += used
            offset = end
            if offset < len(data):
                self._split()

    def _split(self) -> None:
        self.parts.extend(close for _tag, _opening, close in reversed(self.open_tags))
        self.chunks.append("".join(self.parts))
        self.parts = [opening for _tag, opening, _close in self.open_tags]
        self.units = 0

    def finish(self) -> list[str]:
        self.close()
        if self.parts or not self.chunks:
            self.parts.extend(close for _tag, _opening, close in reversed(self.open_tags))
            self.chunks.append("".join(self.parts))
        return self.chunks


class BotMessagingMixin:
    def _write_long_response_artifact(self, text: str) -> Path:
        output = ensure_private_workspace_dir(
            self.config.workspace_path, ".lightclaw-meta", "messages"
        )
        path = output / f"response-{int(time.time())}-{secrets.token_hex(4)}.md"
        atomic_write_text(path, f"{redact_text(text)}\n", mode=0o600)
        return path

    async def _send_response(self, placeholder, update: Update, markdown_response: str):
        """Send bounded Telegram HTML chunks while preserving formatting."""
        if len(markdown_response) > 6000 or self._is_large_code_leak(markdown_response):
            try:
                artifact = self._write_long_response_artifact(markdown_response)
            except (OSError, WorkspaceSafetyError):
                log.warning("Could not save long result under private workspace metadata")
                failure = (
                    "Could not save this long result safely. Check the workspace's "
                    "`.lightclaw-meta` directory."
                )
                if placeholder:
                    await self._try_send(placeholder.edit_text, failure)
                elif update.message:
                    await self._try_send(update.message.reply_text, failure)
                return
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

        html_chunks = _TelegramHTMLChunker(max_len=3000)
        html_chunks.feed(markdown_to_telegram_html(markdown_response))
        rendered_chunks = html_chunks.finish()
        session_id = self._session_id_from_update(update)

        for i, html_chunk in enumerate(rendered_chunks):
            self._log_bot_message(session_id, markdown_response)

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
        if len(rendered_chunks) > 1:
            log.info(f"Long response split into {len(rendered_chunks)} messages ({len(markdown_response)} chars)")

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
        config = getattr(self, "config", None)
        known_values = vars(config) if config is not None else None
        error_text = redact_text(str(err), known_values)
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
            log.warning("[%s] Telegram network issue: %s", session_id, error_text)
            return

        trace = "".join(traceback.format_exception(type(err), err, err.__traceback__))
        log.error(
            "[%s] Unhandled Telegram error:\n%s",
            session_id,
            redact_text(trace, known_values),
        )

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
