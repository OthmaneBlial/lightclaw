"""Markdown to Telegram-safe HTML conversion utilities."""

from __future__ import annotations

import re
from html import escape as _html_escape
from urllib.parse import urlsplit


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return _html_escape(text, quote=True)


def _safe_link_href(destination: str) -> str | None:
    """Return a Telegram-safe web URL, or ``None`` for an unsafe target."""
    parsed = urlsplit(destination.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return destination.strip()


def markdown_to_telegram_html(text: str) -> str:
    """Convert LLM markdown to Telegram-safe HTML.

    Handles code blocks, inline code, bold, italic, strikethrough,
    links, blockquotes, and list markers. All other text is HTML-escaped.
    """
    if not text:
        return ""

    # 1. Extract fenced code blocks → placeholders
    code_blocks: list[str] = []

    def _extract_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r"```\w*\n?([\s\S]*?)```", _extract_code_block, text)

    # 2. Extract inline code → placeholders
    inline_codes: list[str] = []

    def _extract_inline(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", _extract_inline, text)

    # 3. Extract links before escaping so the destination can be validated
    # before it is inserted into an HTML attribute.
    links: list[tuple[str, str]] = []

    def _extract_link(m: re.Match) -> str:
        links.append((m.group(1), m.group(2)))
        return f"\x00LK{len(links) - 1}\x00"

    text = re.sub(r"\[([^\]]+)\]\(([^)]*)\)", _extract_link, text)

    # 4. Strip heading markers (# Title → Title)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"\1", text, flags=re.MULTILINE)

    # 5. Strip blockquote markers
    text = re.sub(r"^>\s*(.*)$", r"\1", text, flags=re.MULTILINE)

    # 6. Escape HTML in remaining text
    text = _escape_html(text)

    # 7. Convert markdown formatting (order matters)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)  # bold
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)  # bold alt
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<i>\1</i>", text)  # italic
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)  # strikethrough
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)  # list markers

    # 8. Restore validated links and inline code.
    for i, (label, destination) in enumerate(links):
        href = _safe_link_href(destination)
        rendered = _escape_html(label)
        if href is not None:
            rendered = f'<a href="{_escape_html(href)}">{rendered}</a>'
        text = text.replace(f"\x00LK{i}\x00", rendered)

    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00IC{i}\x00", f"<code>{_escape_html(code)}</code>")

    # 8. Restore code blocks
    for i, code in enumerate(code_blocks):
        text = text.replace(
            f"\x00CB{i}\x00", f"<pre><code>{_escape_html(code)}</code></pre>"
        )

    return text
