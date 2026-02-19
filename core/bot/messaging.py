"""Telegram message chunking/sending and framework error handling."""

from __future__ import annotations

import re
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Conflict, NetworkError, RetryAfter, TimedOut
from telegram.ext import ContextTypes

from ..logging_setup import log
from ..markdown import markdown_to_telegram_html


class BotMessagingMixin:
    @staticmethod
    def _chunk_message(text: str, max_len: int = 3500) -> list[str]:
        """Split a long message into chunks that fit Telegram's limit.

        Splits at newline boundaries to avoid breaking HTML tags or words.
        Uses 3500 instead of 4096 to leave room for HTML entity expansion
        (< becomes &lt;, > becomes &gt;, etc. which can ~2-3x the size).
        """
        if len(text) <= max_len:
            return [text]

        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break

            # Find the last newline within the limit
            split_at = text.rfind("\n", 0, max_len)
            if split_at <= 0:
                # No newline found — split at max_len (last resort)
                split_at = max_len

            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")

        return chunks

    async def _send_response(self, placeholder, update: Update, markdown_response: str):
        """Send the response, chunking if needed, then convert to HTML.

        Chunks BEFORE HTML conversion to account for entity expansion.
        """
        # First chunk the markdown (before HTML conversion which expands entities)
        markdown_chunks = self._chunk_message(markdown_response, max_len=3000)
        chat_id = update.effective_chat.id if update.effective_chat else 0
        session_id = str(chat_id)

        for i, markdown_chunk in enumerate(markdown_chunks):
            self._log_bot_message(session_id, markdown_chunk)
            # Convert each chunk to HTML separately
            html_chunk = markdown_to_telegram_html(markdown_chunk)

            # Safety check: if HTML conversion made it too long, truncate
            if len(html_chunk) > 4096:
                html_chunk = html_chunk[:4050] + "..."

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
        """Try to send/edit with HTML, fall back to plain text. Returns True on success."""
        try:
            await send_fn(text, parse_mode=ParseMode.HTML)
            return True
        except Exception:
            pass

        # Fallback: strip HTML tags and send as plain text
        try:
            plain = re.sub(r"<[^>]+>", "", text)
            await send_fn(plain)
            return True
        except Exception as e:
            log.error(f"Failed to send message chunk: {e}")
            return False

