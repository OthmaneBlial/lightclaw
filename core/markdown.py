"""Markdown to Telegram-safe HTML conversion utilities."""

from __future__ import annotations

import re
from html import escape as _html_escape
from urllib.parse import urlsplit


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return _html_escape(text, quote=True)


def _safe_link_href(destination: str) -> str | None:
    """Return an absolute HTTP(S) URL, or ``None`` for an unsafe target."""
    href = destination.strip()
    if not href or any(char.isspace() or ord(char) < 32 for char in href):
        return None
    try:
        parsed = urlsplit(href)
        _port = parsed.port  # Access validates malformed and out-of-range ports.
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (_port is None and parsed.netloc.endswith(":"))
    ):
        return None
    return href


def markdown_to_telegram_html(text: str) -> str:
    """Convert LLM markdown to Telegram-safe HTML.

    Handles code blocks, inline code, bold, italic, strikethrough,
    links, blockquotes, and list markers. All other text is HTML-escaped.
    """
    if not text:
        return ""

    # Avoid interpreting user-supplied text that happens to match a placeholder.
    placeholder_prefix = "\x00LCLAW"
    while placeholder_prefix in text:
        placeholder_prefix += "\x00"

    def _placeholder(kind: str, index: int) -> str:
        return f"{placeholder_prefix}{kind}{index}\x00"

    # 1. Extract fenced code blocks → placeholders
    code_blocks: list[str] = []

    def _extract_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return _placeholder("CB", len(code_blocks) - 1)

    text = re.sub(r"```\w*\n?([\s\S]*?)```", _extract_code_block, text)

    # 2. Extract inline code → placeholders
    inline_codes: list[str] = []

    def _extract_inline(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return _placeholder("IC", len(inline_codes) - 1)

    text = re.sub(r"`([^`]+)`", _extract_inline, text)

    # Validate destinations before putting any model-authored value in an attribute.
    links: list[tuple[str, str, str]] = []

    def _extract_link(m: re.Match) -> str:
        links.append((m.group(0), m.group(1), m.group(2)))
        return _placeholder("LK", len(links) - 1)

    text = re.sub(r"\[([^\]]+)\]\(([^)]*)\)", _extract_link, text)

    # 3. Strip heading markers (# Title → Title)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"\1", text, flags=re.MULTILINE)

    # 4. Strip blockquote markers
    text = re.sub(r"^>\s*(.*)$", r"\1", text, flags=re.MULTILINE)

    # 5. Escape HTML in remaining text
    text = _escape_html(text)

    # 6. Restore escaped links before formatting their labels.
    for i, (raw, label, destination) in enumerate(links):
        href = _safe_link_href(destination)
        if href is None:
            rendered = _escape_html(raw)
        else:
            rendered = f'<a href="{_escape_html(href)}">{_escape_html(label)}</a>'
        text = text.replace(_placeholder("LK", i), rendered)

    # 7. Convert markdown formatting (order matters)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)  # bold
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)  # bold alt
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<i>\1</i>", text)  # italic
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)  # strikethrough
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)  # list markers

    # 8. Restore inline code
    for i, code in enumerate(inline_codes):
        text = text.replace(_placeholder("IC", i), f"<code>{_escape_html(code)}</code>")

    # 9. Restore code blocks
    for i, code in enumerate(code_blocks):
        text = text.replace(
            _placeholder("CB", i), f"<pre><code>{_escape_html(code)}</code></pre>"
        )

    return text
