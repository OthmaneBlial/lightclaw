"""Markdown to Telegram-safe HTML conversion utilities."""

from __future__ import annotations

import re


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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

    # 3. Strip heading markers (# Title → Title)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"\1", text, flags=re.MULTILINE)

    # 4. Strip blockquote markers
    text = re.sub(r"^>\s*(.*)$", r"\1", text, flags=re.MULTILINE)

    # 5. Escape HTML in remaining text
    text = _escape_html(text)

    # 6. Convert markdown formatting (order matters)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)  # links
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)  # bold
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)  # bold alt
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<i>\1</i>", text)  # italic
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)  # strikethrough
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)  # list markers

    # 7. Restore inline code
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00IC{i}\x00", f"<code>{_escape_html(code)}</code>")

    # 8. Restore code blocks
    for i, code in enumerate(code_blocks):
        text = text.replace(
            f"\x00CB{i}\x00", f"<pre><code>{_escape_html(code)}</code></pre>"
        )

    return text
