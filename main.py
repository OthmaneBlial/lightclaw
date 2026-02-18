#!/usr/bin/env python3
"""
LightClaw — Minimal AI Agent for Telegram
==========================================
A minimalist Python AI agent. Telegram-only, 5 LLM providers, 
infinite memory, and customizable personality.

Architecture: Telegram Polling → handle_message → Memory Recall → LLM → Reply

Phase 2 features:
  - Markdown → HTML conversion for Telegram
  - "Thinking… 💭" placeholder with edit
  - Session summarization + context window management
  - Personality system (runtime workspace personality files)
  - Voice message transcription (Groq Whisper)
  - Photo/document handling
  - /show command + error handling
  - Orphan message cleanup
"""

import asyncio
import difflib
import io
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Config, load_config
from memory import MemoryStore
from providers import LLMClient
from skills import SkillError, SkillManager

# Project root for resolving runtime-relative paths reliably.
PROJECT_ROOT = Path(__file__).resolve().parent

# ──────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lightclaw")


# ──────────────────────────────────────────────────────────────
# Markdown → Telegram HTML Converter
# ──────────────────────────────────────────────────────────────
# Robust Markdown-to-HTML converter for Telegram
# Telegram's HTML mode supports: <b>, <i>, <s>, <code>, <pre>, <a>


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


def resolve_runtime_path(path_value: str) -> Path:
    """Resolve configured paths relative to LIGHTCLAW_HOME or project root."""
    runtime_home = os.getenv("LIGHTCLAW_HOME", "").strip()
    base_dir = (
        Path(runtime_home).expanduser().resolve()
        if runtime_home
        else PROJECT_ROOT
    )
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


# ──────────────────────────────────────────────────────────────
# File Operation Rules
# ──────────────────────────────────────────────────────────────

FILE_IO_RULES = """## File Operations (CRITICAL)
When creating NEW files (HTML, Python, CSS, Java, etc.):
- ALWAYS use this EXACT format: ```python:filename.py
- Put the filename AFTER the language with a colon: ```lang:filename.ext
- Examples: ```html:landing.html, ```python:script.py, ```css:style.css
- The file will be saved to the runtime workspace directory
- DO NOT include explanatory text inside code blocks - only the actual code
- If the output is large code, ALWAYS save to files and keep chat reply short.
- Never dump full source code in chat when files are created.

When editing EXISTING files:
- ALWAYS use this EXACT format:
```edit:path/to/file.ext
<<<<<<< SEARCH
exact old text from the file
=======
new text
>>>>>>> REPLACE
```
- SEARCH text must match the file exactly.
- SEARCH text must be unique (only one match).
- You may include multiple SEARCH/REPLACE hunks in one edit block.
- Keep file paths relative to the runtime workspace root.
- After edits, provide only a short summary (not full diff body)."""


# ──────────────────────────────────────────────────────────────
# Personality System (runtime workspace loader)
# ──────────────────────────────────────────────────────────────

FALLBACK_IDENTITY = f"""# LightClaw 🦞

You are LightClaw, a helpful, intelligent AI assistant with infinite memory.
You remember all past conversations and can recall context from previous sessions.

## Important Rules
1. Be helpful, accurate, and concise.
2. When you remember something from a past conversation, mention it naturally.
3. If you're unsure about a recalled memory, say so.
4. Respond in the same language the user writes in.

{FILE_IO_RULES}

## Example good response:
"Here's your landing page:
```html:index.html
<!DOCTYPE html>
<html>
...
</html>
```
Done! A modern book-selling landing page."
"""


def load_personality(workspace_path: str) -> str:
    """Load personality from runtime workspace files (SOUL.md, IDENTITY.md, USER.md).

    Falls back to a hardcoded identity if no files exist.
    """
    files = ["IDENTITY.md", "SOUL.md", "USER.md"]
    parts = []

    for filename in files:
        filepath = Path(workspace_path) / filename
        if filepath.exists():
            try:
                content = filepath.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(content)
            except Exception:
                pass

    if not parts:
        return FALLBACK_IDENTITY

    return "\n\n---\n\n".join(parts)


# ──────────────────────────────────────────────────────────────
# System Prompt Builder
# ──────────────────────────────────────────────────────────────


def build_system_prompt(
    config: Config,
    personality: str,
    memories_text: str,
    session_summary: str,
    skills_text: str = "",
) -> str:
    """Build the full system prompt with identity, memories, and summary."""
    parts = [
        personality,
        f"## Current Time\n{datetime.now().strftime('%Y-%m-%d %H:%M (%A)')}",
        f"## Provider\n{config.llm_provider} ({config.llm_model})",
        FILE_IO_RULES,
    ]

    if memories_text:
        parts.append(memories_text)

    if session_summary:
        parts.append(f"## Summary of Previous Conversation\n\n{session_summary}")

    if skills_text:
        parts.append(skills_text)

    return "\n\n---\n\n".join(parts)


# ──────────────────────────────────────────────────────────────
# Voice Transcription (Groq Whisper)
# ──────────────────────────────────────────────────────────────


async def transcribe_voice(audio_bytes: bytes, groq_api_key: str) -> str | None:
    """Transcribe audio using Groq's Whisper API. Returns text or None on failure."""
    if not groq_api_key:
        return None

    try:
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {groq_api_key}"},
                files={"file": ("audio.ogg", audio_bytes, "audio/ogg")},
                data={"model": "whisper-large-v3-turbo"},
            )
            if response.status_code == 200:
                return response.json().get("text", "")
    except ImportError:
        log.warning("httpx not installed — voice transcription unavailable. pip install httpx")
    except Exception as e:
        log.error(f"Voice transcription failed: {e}")

    return None


# ──────────────────────────────────────────────────────────────
# Telegram Bot Handlers
# ──────────────────────────────────────────────────────────────


@dataclass
class FileOperationResult:
    action: str
    path: str
    detail: str = ""
    diff: str = ""


class LightClawBot:
    """The main bot class wiring Telegram, Memory, and LLM together."""

    def __init__(self, config: Config):
        self.config = config
        self.memory = MemoryStore(config.memory_db_path)
        self.llm = LLMClient(config)
        self.skills = SkillManager(
            workspace_path=config.workspace_path,
            skills_state_path=config.skills_state_path,
            hub_base_url=config.skills_hub_base_url,
        )
        self.personality = load_personality(config.workspace_path)
        self.start_time = time.time()

        # Per-session summaries (in-memory, persisted via memory.py)
        self._session_summaries: dict[str, str] = {}
        # Lock to prevent concurrent summarization per session
        self._summarizing: set[str] = set()
        # Confirmation window for destructive memory wipe command (per chat).
        self._pending_wipe_confirm: dict[str, float] = {}

    def is_allowed(self, user_id: int) -> bool:
        """Check if this user is in the allowlist (empty = allow all)."""
        if not self.config.telegram_allowed_users:
            return True
        return str(user_id) in self.config.telegram_allowed_users

    @staticmethod
    def _session_id_from_update(update: Update | None) -> str:
        if update and update.effective_chat:
            return str(update.effective_chat.id)
        return "unknown"

    @staticmethod
    def _trim_for_log(text: str, max_chars: int = 8000) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n...[truncated]"

    @staticmethod
    def _strip_html_for_log(text: str) -> str:
        return re.sub(r"<[^>]+>", "", text)

    def _log_user_message(self, session_id: str, text: str):
        log.info(f"[{session_id}] User: {self._trim_for_log(text)}")

    def _log_bot_message(self, session_id: str, text: str):
        log.info(f"[{session_id}] Bot: {self._trim_for_log(text)}")

    async def _reply_logged(
        self,
        update: Update,
        text: str,
        parse_mode: str | None = None,
    ):
        """Reply to Telegram and mirror the same content to terminal logs."""
        session_id = self._session_id_from_update(update)
        logged_text = self._strip_html_for_log(text) if parse_mode == ParseMode.HTML else text
        self._log_bot_message(session_id, logged_text)

        if parse_mode:
            return await update.message.reply_text(text, parse_mode=parse_mode)
        return await update.message.reply_text(text)

    # ── Token Estimation ─────────────────────────────────────

    @staticmethod
    def estimate_tokens(messages: list[dict]) -> int:
        """Estimate token count using a 2.5 chars/token heuristic."""
        total_chars = sum(len(m.get("content", "")) for m in messages)
        return total_chars * 2 // 5

    # ── Session Summarization ────────────────────────────────

    async def maybe_summarize(self, session_id: str):
        """Trigger summarization if history is too long or token count too high."""
        recent = self.memory.get_recent(session_id, limit=100)
        token_estimate = self.estimate_tokens(recent)
        threshold = self.config.context_window * 75 // 100

        if len(recent) <= 20 and token_estimate <= threshold:
            return

        if session_id in self._summarizing:
            return
        self._summarizing.add(session_id)

        try:
            await self._summarize_session(session_id, recent)
        finally:
            self._summarizing.discard(session_id)

    async def _summarize_session(self, session_id: str, history: list[dict]):
        """Use the LLM to summarize older messages, keep last 4."""
        if len(history) <= 4:
            return

        to_summarize = history[:-4]

        # Filter to user/assistant only, skip oversized messages
        max_msg_tokens = self.config.context_window // 2
        valid = [
            m for m in to_summarize
            if m.get("role") in ("user", "assistant")
            and len(m.get("content", "")) * 2 // 5 <= max_msg_tokens
        ]

        if not valid:
            return

        existing_summary = self._session_summaries.get(session_id, "")

        # Build summarization prompt
        prompt = "Provide a concise summary of this conversation, preserving key context and important points.\n"
        if existing_summary:
            prompt += f"Existing context: {existing_summary}\n"
        prompt += "\nCONVERSATION:\n"
        for m in valid:
            prompt += f"{m['role']}: {m['content']}\n"

        try:
            summary = await self.llm.chat(
                [{"role": "user", "content": prompt}],
                system_prompt="You are a conversation summarizer. Be concise but preserve all important context.",
            )
            if summary:
                self._session_summaries[session_id] = summary
                log.info(f"[{session_id}] Summarized {len(valid)} messages → {len(summary)} chars")
        except Exception as e:
            log.error(f"Summarization failed: {e}")

    def _get_session_summary(self, session_id: str) -> str:
        """Get the stored summary for a session."""
        # First check in-memory cache
        if session_id in self._session_summaries:
            return self._session_summaries[session_id]
        # Fall back to memory store
        return self.memory.get_summary(session_id)

    # ── Emergency Context Compression ────────────────────────

    def _is_context_error(self, error_msg: str) -> bool:
        """Detect context window overflow errors from LLM providers."""
        lower = error_msg.lower()
        return any(kw in lower for kw in ("token", "context", "length", "too long", "too large"))

    # ── Orphan Tool Message Cleanup ──────────────────────────

    @staticmethod
    def _clean_orphan_messages(messages: list[dict]) -> list[dict]:
        """Strip leading 'tool' role messages that lack a preceding assistant tool call.

        # Prevent potential issue where tool roles are orphans
        """
        while messages and messages[0].get("role") == "tool":
            messages = messages[1:]
        return messages

    # ── /start ────────────────────────────────────────────────

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        session_id = self._session_id_from_update(update)
        self._log_user_message(session_id, "/start")
        await self._reply_logged(
            update,
            "🦞 <b>LightClaw</b> is ready!\n\n"
            "I'm your AI assistant with infinite memory. "
            "I remember everything we've talked about, even across sessions.\n\n"
            "<b>Commands:</b>\n"
            "/help - Show this message\n"
            "/clear - Reset our conversation\n"
            "/wipe_memory - Wipe ALL memory (with confirmation)\n"
            "/memory - Show memory stats\n"
            "/recall &lt;query&gt; - Search my memories\n"
            "/skills - Manage skills (install/use/create)\n"
            "/show - Show current config",
            parse_mode=ParseMode.HTML,
        )

    # ── /help ─────────────────────────────────────────────────

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        session_id = self._session_id_from_update(update)
        self._log_user_message(session_id, "/help")
        await self._reply_logged(
            update,
            "🦞 <b>LightClaw Commands</b>\n\n"
            "/start - Welcome message\n"
            "/help - This help message\n"
            "/clear - Clear conversation history\n"
            "/wipe_memory - Wipe ALL memory (dangerous)\n"
            "/memory - Show memory statistics\n"
            "/recall &lt;query&gt; - Search past conversations\n"
            "/skills - Install/use/create skills\n"
            "/show - Show current model, provider, uptime",
            parse_mode=ParseMode.HTML,
        )

    # ── /clear ────────────────────────────────────────────────

    async def cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        session_id = str(update.effective_chat.id) if update.effective_chat else "unknown"
        self._log_user_message(session_id, "/clear")
        self.memory.clear_session(session_id)
        self._session_summaries.pop(session_id, None)
        await self._reply_logged(
            update,
            "🗑️ Conversation cleared. Your memories from this chat have been reset.\n"
            "Note: memories from other chats are preserved."
        )

    # ── /wipe_memory ─────────────────────────────────────────

    async def cmd_wipe_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Dangerous command: wipe all memory after explicit confirmation."""
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        session_id = str(update.effective_chat.id) if update.effective_chat else "unknown"
        args = [a.strip().lower() for a in (context.args or []) if a.strip()]
        self._log_user_message(session_id, f"/wipe_memory {' '.join(args)}".strip())

        now = time.time()
        confirm_window_sec = 90
        pending_until = self._pending_wipe_confirm.get(session_id, 0.0)

        if args and args[0] in {"confirm", "yes", "now"}:
            if pending_until and now <= pending_until:
                await asyncio.to_thread(self.memory.clear_all)
                self._session_summaries.clear()
                self._pending_wipe_confirm.pop(session_id, None)
                await self._reply_logged(
                    update,
                    "🧨 <b>All memory wiped.</b>\n"
                    "All sessions/interactions were deleted. The bot now starts fresh.",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await self._reply_logged(
                    update,
                    "No active wipe confirmation.\n"
                    "Run <code>/wipe_memory</code> first, then confirm within 90s with "
                    "<code>/wipe_memory confirm</code>.",
                    parse_mode=ParseMode.HTML,
                )
            return

        self._pending_wipe_confirm[session_id] = now + confirm_window_sec
        await self._reply_logged(
            update,
            "⚠️ <b>Danger: wipe ALL memory</b>\n"
            "This deletes every saved interaction and session across all chats.\n\n"
            f"To confirm within {confirm_window_sec}s, run:\n"
            "<code>/wipe_memory confirm</code>",
            parse_mode=ParseMode.HTML,
        )

    # ── /memory ───────────────────────────────────────────────

    async def cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        session_id = self._session_id_from_update(update)
        self._log_user_message(session_id, "/memory")
        stats = self.memory.stats()
        await self._reply_logged(
            update,
            f"🧠 <b>Memory Stats</b>\n\n"
            f"📝 Total interactions: {stats['total_interactions']}\n"
            f"💬 Unique sessions: {stats['unique_sessions']}\n"
            f"📚 Vocabulary size: {stats['vocabulary_size']}",
            parse_mode=ParseMode.HTML,
        )

    # ── /recall ───────────────────────────────────────────────

    async def cmd_recall(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        query = " ".join(context.args) if context.args else ""
        session_id = self._session_id_from_update(update)
        self._log_user_message(session_id, f"/recall {query}".strip())
        if not query:
            await self._reply_logged(
                update,
                "Usage: /recall &lt;search query&gt;",
                parse_mode=ParseMode.HTML,
            )
            return

        memories = self.memory.recall(query, top_k=5)
        if not memories:
            await self._reply_logged(update, "🔍 No matching memories found.")
            return

        lines = [f"🔍 <b>Top {len(memories)} memories for:</b> <i>{_escape_html(query)}</i>\n"]
        for i, m in enumerate(memories, 1):
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.timestamp))
            score = f"{m.similarity:.0%}"
            preview = _escape_html(m.content[:100])
            lines.append(f"{i}. [{ts}] ({score}) {m.role}: {preview}")

        await self._reply_logged(update, "\n".join(lines), parse_mode=ParseMode.HTML)

    # ── /skills ───────────────────────────────────────────────

    @staticmethod
    def _skills_usage_text() -> str:
        return (
            "<b>Usage</b>\n"
            "<code>/skills</code> - list installed + active skills\n"
            "<code>/skills search &lt;query&gt;</code> - search ClawHub\n"
            "<code>/skills add &lt;slug|owner/slug|url|slug@version&gt;</code> - install from ClawHub\n"
            "<code>/skills use &lt;id&gt;</code> - activate skill in this chat\n"
            "<code>/skills off &lt;id&gt;</code> - deactivate skill in this chat\n"
            "<code>/skills create &lt;name&gt; [description]</code> - create local skill\n"
            "<code>/skills show &lt;id&gt;</code> - preview SKILL.md\n"
            "<code>/skills remove &lt;id&gt;</code> - uninstall skill"
        )

    def _render_skills_overview(self, session_id: str) -> str:
        installed = self.skills.list_skills()
        active = self.skills.active_records(session_id)
        active_ids = {s.skill_id for s in active}

        lines = ["🧩 <b>Skills</b>", ""]
        if active:
            lines.append(
                "<b>Active in this chat:</b> "
                + ", ".join(f"<code>{_escape_html(s.skill_id)}</code>" for s in active)
            )
        else:
            lines.append("<b>Active in this chat:</b> none")

        lines.append(f"<b>Installed:</b> {len(installed)}")
        lines.append("")

        if installed:
            lines.append("<b>Installed skills</b>")
            for skill in installed:
                marker = "✅ " if skill.skill_id in active_ids else ""
                desc = _escape_html((skill.description or "").strip())
                if len(desc) > 90:
                    desc = desc[:87] + "..."
                version = f" v{_escape_html(skill.version)}" if skill.version else ""
                lines.append(
                    f"• {marker}<code>{_escape_html(skill.skill_id)}</code> "
                    f"({skill.source}{version}) - {_escape_html(skill.name)}"
                )
                if desc:
                    lines.append(f"  {desc}")
        else:
            lines.append("No skills installed yet.")

        lines.append("")
        lines.append(self._skills_usage_text())
        return "\n".join(lines)

    async def cmd_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        session_id = str(update.effective_chat.id) if update.effective_chat else "unknown"
        args = context.args or []
        self._log_user_message(session_id, f"/skills {' '.join(args)}".strip())
        sub = args[0].lower() if args else "list"

        if sub in {"list", "ls"} and len(args) == 1:
            sub = "list"

        if sub == "list":
            text = await asyncio.to_thread(self._render_skills_overview, session_id)
            await self._reply_logged(update, text, parse_mode=ParseMode.HTML)
            return

        if sub in {"search", "find"}:
            query = " ".join(args[1:]).strip() if len(args) > 1 else ""
            if not query:
                await self._reply_logged(
                    update,
                    "Usage: <code>/skills search &lt;query&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            try:
                results = await asyncio.to_thread(self.skills.search_hub, query, 8)
            except SkillError as e:
                await self._reply_logged(
                    update,
                    f"⚠️ Search failed: {_escape_html(str(e))}",
                    parse_mode=ParseMode.HTML,
                )
                return

            if not results:
                await self._reply_logged(
                    update,
                    f"No skills found for <code>{_escape_html(query)}</code>.",
                    parse_mode=ParseMode.HTML,
                )
                return

            lines = [f"🔎 <b>ClawHub results</b> for <code>{_escape_html(query)}</code>", ""]
            for item in results:
                version = f" v{_escape_html(item.version)}" if item.version else ""
                summary = _escape_html(item.summary or "")
                if len(summary) > 110:
                    summary = summary[:107] + "..."
                lines.append(
                    f"• <code>{_escape_html(item.slug)}</code> - {_escape_html(item.display_name)}{version}"
                )
                if summary:
                    lines.append(f"  {summary}")
            lines.append("")
            lines.append("Install: <code>/skills add &lt;slug&gt;</code>")
            await self._reply_logged(update, "\n".join(lines), parse_mode=ParseMode.HTML)
            return

        if sub in {"add", "install", "grab"}:
            if len(args) < 2:
                await self._reply_logged(
                    update,
                    "Usage: <code>/skills add &lt;slug|owner/slug|url|slug@version&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            target = args[1]
            version = args[2].strip() if len(args) > 2 else None
            progress = await self._reply_logged(update, "Installing skill from ClawHub...")

            try:
                skill, replaced = await asyncio.to_thread(
                    self.skills.install_from_hub, target, version
                )
                await asyncio.to_thread(self.skills.activate, session_id, skill.skill_id)
            except SkillError as e:
                fail_text = f"⚠️ Install failed: {_escape_html(str(e))}"
                self._log_bot_message(session_id, self._strip_html_for_log(fail_text))
                await progress.edit_text(
                    fail_text,
                    parse_mode=ParseMode.HTML,
                )
                return

            action = "Updated" if replaced else "Installed"
            lines = [
                f"✅ {action} <code>{_escape_html(skill.skill_id)}</code>",
                f"Name: {_escape_html(skill.name)}",
            ]
            if skill.version:
                lines.append(f"Version: <code>{_escape_html(skill.version)}</code>")
            lines.extend(
                [
                    "",
                    "Auto-activated for this chat.",
                    "List skills: <code>/skills</code>",
                ]
            )
            success_text = "\n".join(lines)
            self._log_bot_message(session_id, self._strip_html_for_log(success_text))
            await progress.edit_text(success_text, parse_mode=ParseMode.HTML)
            return

        if sub in {"use", "enable", "on"}:
            if len(args) < 2:
                await self._reply_logged(
                    update,
                    "Usage: <code>/skills use &lt;id&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            ref = args[1]
            skill = await asyncio.to_thread(self.skills.resolve_skill, ref)
            if not skill:
                await self._reply_logged(
                    update,
                    f"⚠️ Skill not found: <code>{_escape_html(ref)}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            await asyncio.to_thread(self.skills.activate, session_id, skill.skill_id)
            await self._reply_logged(
                update,
                f"✅ Activated <code>{_escape_html(skill.skill_id)}</code> for this chat.",
                parse_mode=ParseMode.HTML,
            )
            return

        if sub in {"off", "disable", "unuse"}:
            if len(args) < 2:
                await self._reply_logged(
                    update,
                    "Usage: <code>/skills off &lt;id&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            ref = args[1]
            skill = await asyncio.to_thread(self.skills.resolve_skill, ref)
            if not skill:
                await self._reply_logged(
                    update,
                    f"⚠️ Skill not found: <code>{_escape_html(ref)}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            await asyncio.to_thread(self.skills.deactivate, session_id, skill.skill_id)
            await self._reply_logged(
                update,
                f"✅ Deactivated <code>{_escape_html(skill.skill_id)}</code> for this chat.",
                parse_mode=ParseMode.HTML,
            )
            return

        if sub in {"create", "new"}:
            if len(args) < 2:
                await self._reply_logged(
                    update,
                    "Usage: <code>/skills create &lt;name&gt; [description]</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            name = args[1]
            description = " ".join(args[2:]).strip() if len(args) > 2 else ""
            try:
                skill = await asyncio.to_thread(self.skills.create_local_skill, name, description)
                await asyncio.to_thread(self.skills.activate, session_id, skill.skill_id)
            except SkillError as e:
                await self._reply_logged(
                    update,
                    f"⚠️ Create failed: {_escape_html(str(e))}",
                    parse_mode=ParseMode.HTML,
                )
                return

            rel_path = f"skills/local/{skill.directory.name}/SKILL.md"
            await self._reply_logged(
                update,
                "\n".join(
                    [
                        f"✅ Created local skill <code>{_escape_html(skill.skill_id)}</code>",
                        f"File: <code>{_escape_html(rel_path)}</code>",
                        "Auto-activated for this chat.",
                    ]
                ),
                parse_mode=ParseMode.HTML,
            )
            return

        if sub in {"show", "view"}:
            if len(args) < 2:
                await self._reply_logged(
                    update,
                    "Usage: <code>/skills show &lt;id&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            ref = args[1]
            skill = await asyncio.to_thread(self.skills.resolve_skill, ref)
            if not skill:
                await self._reply_logged(
                    update,
                    f"⚠️ Skill not found: <code>{_escape_html(ref)}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            try:
                content = await asyncio.to_thread(
                    skill.skill_path.read_text, "utf-8", "replace"
                )
            except Exception as e:
                await self._reply_logged(
                    update,
                    f"⚠️ Failed to read skill: {_escape_html(str(e))}",
                    parse_mode=ParseMode.HTML,
                )
                return

            max_chars = 2000
            preview = content.strip()
            truncated = False
            if len(preview) > max_chars:
                preview = preview[:max_chars].rstrip()
                truncated = True

            msg = (
                f"🧩 <b>{_escape_html(skill.name)}</b> "
                f"(<code>{_escape_html(skill.skill_id)}</code>)\n"
                f"<pre>{_escape_html(preview)}</pre>"
            )
            if truncated:
                msg += "\n<i>Preview truncated.</i>"
            await self._reply_logged(update, msg, parse_mode=ParseMode.HTML)
            return

        if sub in {"remove", "delete", "rm", "uninstall"}:
            if len(args) < 2:
                await self._reply_logged(
                    update,
                    "Usage: <code>/skills remove &lt;id&gt;</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            ref = args[1]
            try:
                removed = await asyncio.to_thread(self.skills.remove_skill, ref)
            except SkillError as e:
                await self._reply_logged(
                    update,
                    f"⚠️ Remove failed: {_escape_html(str(e))}",
                    parse_mode=ParseMode.HTML,
                )
                return

            await self._reply_logged(
                update,
                f"🗑️ Removed <code>{_escape_html(removed.skill_id)}</code>.",
                parse_mode=ParseMode.HTML,
            )
            return

        await self._reply_logged(
            update,
            "Unknown /skills subcommand.\n\n" + self._skills_usage_text(),
            parse_mode=ParseMode.HTML,
        )

    # ── File Operation Helpers ────────────────────────────────

    def _resolve_workspace_path(self, raw_path: str) -> tuple[Path | None, str | None, str | None]:
        """Resolve a user-provided path inside workspace, blocking traversal."""
        path_text = raw_path.strip().strip("`").strip()
        if not path_text:
            return None, None, "empty path"
        if os.path.isabs(path_text):
            return None, None, "absolute paths are not allowed"

        workspace = Path(self.config.workspace_path).resolve()
        candidate = (workspace / path_text).resolve()
        try:
            rel = candidate.relative_to(workspace)
        except ValueError:
            return None, None, "path is outside workspace/"

        if str(rel) == ".":
            return None, None, "path points to workspace root"

        return candidate, rel.as_posix(), None

    def _workspace_display_path(self) -> str:
        """Human-friendly workspace path for status messages."""
        workspace = Path(self.config.workspace_path).resolve()
        runtime_home = os.getenv("LIGHTCLAW_HOME", "").strip()
        if runtime_home:
            try:
                rel = workspace.relative_to(Path(runtime_home).expanduser().resolve())
                return rel.as_posix()
            except ValueError:
                pass
        return workspace.as_posix()

    @staticmethod
    def _build_unified_diff(before: str, after: str, rel_path: str) -> str:
        """Build a unified diff between old and new content."""
        diff_lines = difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            lineterm="\n",
        )
        return "".join(diff_lines).strip()

    @staticmethod
    def _apply_search_replace_hunks(content: str, edit_body: str) -> tuple[str, str | None]:
        """Apply SEARCH/REPLACE hunks with exact-match + unique-match semantics."""
        hunk_pattern = re.compile(
            r"<<<<<<<\s*SEARCH\r?\n([\s\S]*?)\r?\n=======\r?\n([\s\S]*?)\r?\n>>>>>>>\s*REPLACE",
            re.MULTILINE,
        )
        matches = list(hunk_pattern.finditer(edit_body))
        if not matches:
            return content, "no SEARCH/REPLACE hunks found"

        updated = content
        for idx, match in enumerate(matches, 1):
            old_text = match.group(1)
            new_text = match.group(2)

            if old_text == "":
                return content, f"hunk {idx}: SEARCH block is empty"

            occurrences = updated.count(old_text)
            if occurrences == 0:
                return content, f"hunk {idx}: SEARCH text not found (must match exactly)"
            if occurrences > 1:
                return content, f"hunk {idx}: SEARCH text appears {occurrences} times; add more context"

            updated = updated.replace(old_text, new_text, 1)

        return updated, None

    @staticmethod
    def _diff_line_stats(diff_text: str) -> tuple[int, int]:
        """Return added/deleted line counts from unified diff text."""
        added = 0
        deleted = 0
        for line in diff_text.splitlines():
            if line.startswith("+++ ") or line.startswith("--- "):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                deleted += 1
        return added, deleted

    @staticmethod
    def _compact_response_for_file_ops(text: str) -> str:
        """Compress verbose model prose when files were created/edited."""
        if not text:
            return ""

        compact = re.sub(r"\[File (saved|updated|edited): [^\]]+\]", "", text)
        compact = re.sub(r"\[No changes: [^\]]+\]", "", compact)
        compact = re.sub(r"```[\s\S]*?```", "", compact)
        compact = re.sub(r"\n{3,}", "\n\n", compact).strip()
        if not compact:
            return "Done."

        if len(compact) > 320 or compact.count("\n") > 6:
            # Keep only the first paragraph for speed/readability in Telegram.
            first = compact.split("\n\n", 1)[0].strip()
            if len(first) > 220:
                first = first[:217].rstrip() + "..."
            return first or "Done."

        return compact

    @staticmethod
    def _render_file_operations(
        operations: list[FileOperationResult],
        include_diffs: bool = True,
        workspace_label: str = "workspace/",
    ) -> str:
        """Render a human-readable summary of applied file operations."""
        if not operations:
            return ""

        success = [op for op in operations if op.action != "error"]
        failures = [op for op in operations if op.action == "error"]
        lines: list[str] = []
        total_added = 0
        total_deleted = 0

        if success:
            lines.append(f"✅ Applied {len(success)} file operation(s):")
            for op in success:
                change_hint = ""
                if op.diff:
                    added, deleted = LightClawBot._diff_line_stats(op.diff)
                    total_added += added
                    total_deleted += deleted
                    if include_diffs:
                        change_hint = f" (+{added}/-{deleted} lines)"

                if op.action in ("created", "auto_created"):
                    lines.append(f"- Created `{op.path}`{change_hint}")
                elif op.action == "updated":
                    lines.append(f"- Updated `{op.path}`{change_hint}")
                elif op.action == "edited":
                    lines.append(f"- Edited `{op.path}`{change_hint}")
                elif op.action == "unchanged":
                    lines.append(f"- No changes in `{op.path}`")
                else:
                    lines.append(f"- `{op.path}`")

            if include_diffs and (total_added or total_deleted):
                lines.append(f"- Diff summary: +{total_added} / -{total_deleted} lines")

            lines.append("")
            lines.append(f"📁 Saved to {workspace_label}")

        if failures:
            if lines:
                lines.append("")
            lines.append(f"⚠️ {len(failures)} file operation(s) failed:")
            for op in failures:
                detail = op.detail or "unknown error"
                lines.append(f"- `{op.path}`: {detail}")

        return "\n".join(lines).strip()

    # ── File Operation Tool ───────────────────────────────────

    async def _process_file_blocks(self, response: str) -> tuple[list[FileOperationResult], str]:
        """Apply edit/create instructions from model response and return cleaned text."""
        operations: list[FileOperationResult] = []
        cleaned_response = response

        lang_extensions = {
            "html": ".html",
            "htm": ".html",
            "css": ".css",
            "javascript": ".js",
            "js": ".js",
            "python": ".py",
            "py": ".py",
            "json": ".json",
            "xml": ".xml",
            "sql": ".sql",
            "markdown": ".md",
            "md": ".md",
            "bash": ".sh",
            "sh": ".sh",
            "txt": ".txt",
            "java": ".java",
            "ts": ".ts",
            "tsx": ".tsx",
            "jsx": ".jsx",
            "go": ".go",
            "rs": ".rs",
            "c": ".c",
            "cpp": ".cpp",
            "yaml": ".yaml",
            "yml": ".yml",
        }

        edit_pattern = re.compile(
            r"```edit:(?P<path>[^\n`]+)\s*\n(?P<body>[\s\S]*?)```",
            re.IGNORECASE,
        )
        edit_blocks_found = bool(edit_pattern.search(cleaned_response))

        def success_count() -> int:
            return sum(1 for op in operations if op.action != "error")

        def write_workspace_file(raw_path: str, content: str, auto_generated: bool = False) -> str:
            target, rel_path, path_err = self._resolve_workspace_path(raw_path)
            display_path = rel_path or raw_path.strip() or "unknown"
            if path_err or target is None or rel_path is None:
                operations.append(FileOperationResult("error", display_path, path_err or "invalid path"))
                return f"[Save failed: {display_path}]"

            before = None
            if target.exists():
                try:
                    before = target.read_text(encoding="utf-8")
                except Exception as e:
                    operations.append(FileOperationResult("error", rel_path, f"failed to read file: {e}"))
                    return f"[Save failed: {rel_path}]"

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            except Exception as e:
                operations.append(FileOperationResult("error", rel_path, f"failed to write file: {e}"))
                return f"[Save failed: {rel_path}]"

            if before is None:
                action = "auto_created" if auto_generated else "created"
                diff_text = self._build_unified_diff("", content, rel_path)
                operations.append(FileOperationResult(action, rel_path, diff=diff_text))
                log.info(f"Saved file: {target}")
                return f"[File saved: {rel_path}]"

            if before == content:
                operations.append(FileOperationResult("unchanged", rel_path))
                return f"[No changes: {rel_path}]"

            diff_text = self._build_unified_diff(before, content, rel_path)
            operations.append(FileOperationResult("updated", rel_path, diff=diff_text))
            log.info(f"Updated file: {target}")
            return f"[File updated: {rel_path}]"

        def apply_edit_block(match: re.Match) -> str:
            raw_path = match.group("path").strip()
            edit_body = match.group("body").strip("\n")

            target, rel_path, path_err = self._resolve_workspace_path(raw_path)
            display_path = rel_path or raw_path or "unknown"
            if path_err or target is None or rel_path is None:
                operations.append(FileOperationResult("error", display_path, path_err or "invalid path"))
                return f"[Edit failed: {display_path}]"

            if not target.exists():
                operations.append(FileOperationResult("error", rel_path, "file not found"))
                return f"[Edit failed: {rel_path}]"

            try:
                before = target.read_text(encoding="utf-8")
            except Exception as e:
                operations.append(FileOperationResult("error", rel_path, f"failed to read file: {e}"))
                return f"[Edit failed: {rel_path}]"

            after, apply_err = self._apply_search_replace_hunks(before, edit_body)
            if apply_err:
                operations.append(FileOperationResult("error", rel_path, apply_err))
                return f"[Edit failed: {rel_path}]"

            if after == before:
                operations.append(FileOperationResult("unchanged", rel_path))
                return f"[No changes: {rel_path}]"

            try:
                target.write_text(after, encoding="utf-8")
            except Exception as e:
                operations.append(FileOperationResult("error", rel_path, f"failed to write file: {e}"))
                return f"[Edit failed: {rel_path}]"

            diff_text = self._build_unified_diff(before, after, rel_path)
            operations.append(FileOperationResult("edited", rel_path, diff=diff_text))
            log.info(f"Applied edit block: {target}")
            return f"[File edited: {rel_path}]"

        cleaned_response = re.sub(edit_pattern, apply_edit_block, cleaned_response)

        pattern_named = re.compile(
            r"```([a-zA-Z0-9_+\-]+):([^\n`]+)\s*\n([\s\S]*?)```",
            re.MULTILINE,
        )

        def apply_named_file_block(match: re.Match) -> str:
            raw_path = match.group(2).strip()
            content = match.group(3).strip()
            return write_workspace_file(raw_path, content)

        cleaned_response = re.sub(pattern_named, apply_named_file_block, cleaned_response)

        # Common malformed style: ```index.html ... ```
        pattern_filename_fence = re.compile(
            r"```(?P<path>[^\n`]+\.[a-zA-Z0-9]{1,10})\s*\n(?P<body>[\s\S]*?)```",
            re.MULTILINE,
        )

        def apply_filename_fence_block(match: re.Match) -> str:
            raw_path = match.group("path").strip()
            content = match.group("body").strip()
            return write_workspace_file(raw_path, content)

        cleaned_response = re.sub(pattern_filename_fence, apply_filename_fence_block, cleaned_response)

        pattern_file_label = re.compile(
            r"File:\s*([^\n`]+)\s*\n```([a-zA-Z0-9_+\-]+)?\s*\n?([\s\S]*?)```",
            re.IGNORECASE,
        )

        def apply_file_label_block(match: re.Match) -> str:
            raw_path = match.group(1).strip()
            content = match.group(3).strip()
            return write_workspace_file(raw_path, content)

        cleaned_response = re.sub(pattern_file_label, apply_file_label_block, cleaned_response)

        file_counter = 1
        pattern_auto = re.compile(r"```([a-zA-Z0-9_+\-]+)?\s*\n([\s\S]*?)```")

        def is_code_like(lang: str, content: str) -> bool:
            if lang and lang not in {"text", "txt", "plain"}:
                return True
            hints = ("<!doctype", "<html", "{", "};", "function ", "class ", "import ", "def ")
            lowered = content.lower()
            return any(h in lowered for h in hints)

        def apply_auto_block(match: re.Match) -> str:
            nonlocal file_counter
            lang = (match.group(1) or "txt").strip().lower()
            content = match.group(2).strip()

            # Keep tiny snippets inline; move large/code-like blocks to workspace.
            if lang == "diff":
                return match.group(0)
            if len(content) < 120 and not is_code_like(lang, content):
                return match.group(0)

            ext = lang_extensions.get(lang, ".txt")
            filename = f"output_{int(time.time())}_{file_counter}{ext}"
            file_counter += 1
            return write_workspace_file(filename, content, auto_generated=True)

        cleaned_response = re.sub(pattern_auto, apply_auto_block, cleaned_response)

        # Salvage malformed/unclosed named fence: ```html:index.html ...EOF
        if success_count() == 0:
            unclosed_named = re.search(
                r"```([a-zA-Z0-9_+\-]+):([^\n`]+)\s*\n([\s\S]+)$",
                cleaned_response,
                re.MULTILINE,
            )
            if unclosed_named:
                raw_path = unclosed_named.group(2).strip()
                content = unclosed_named.group(3).strip()
                if content:
                    marker = write_workspace_file(raw_path, content)
                    prefix = cleaned_response[: unclosed_named.start()].rstrip()
                    cleaned_response = (
                        (prefix + "\n\n" if prefix else "")
                        + marker
                    ).strip()

        # Salvage malformed/unclosed generic fence: ```html ...EOF
        if success_count() == 0:
            unclosed_generic = re.search(
                r"```([a-zA-Z0-9_+\-]+)?\s*\n([\s\S]+)$",
                cleaned_response,
                re.MULTILINE,
            )
            if unclosed_generic:
                lang = (unclosed_generic.group(1) or "txt").strip().lower()
                content = unclosed_generic.group(2).strip()
                if lang != "diff" and len(content) >= 120:
                    ext = lang_extensions.get(lang, ".txt")
                    filename = f"output_{int(time.time())}_unclosed{ext}"
                    marker = write_workspace_file(filename, content, auto_generated=True)
                    prefix = cleaned_response[: unclosed_generic.start()].rstrip()
                    cleaned_response = (
                        (prefix + "\n\n" if prefix else "")
                        + marker
                    ).strip()

        # Last resort: HTML document without fences.
        if success_count() == 0:
            html_start = cleaned_response.lower().find("<!doctype html")
            if html_start < 0:
                html_start = cleaned_response.lower().find("<html")
            if html_start >= 0:
                html = cleaned_response[html_start:].strip()
                if len(html) >= 200:
                    marker = write_workspace_file("index.html", html, auto_generated=True)
                    intro = cleaned_response[:html_start].strip()
                    cleaned_response = (f"{intro}\n\n{marker}" if intro else marker).strip()

        # Never return huge fenced code in chat: remove any remaining large code blocks.
        def strip_large_leftover_code(match: re.Match) -> str:
            content = match.group(2).strip()
            if len(content) >= 120:
                return "[Large code omitted in chat]"
            return match.group(0)

        cleaned_response = re.sub(pattern_auto, strip_large_leftover_code, cleaned_response)

        return operations, cleaned_response.strip()

    async def _retry_failed_edits(
        self,
        user_text: str,
        original_model_response: str,
        failed_ops: list[FileOperationResult],
    ) -> tuple[list[FileOperationResult], str]:
        """Retry failed edit operations once using exact current file content."""
        retryable_errors = [
            op for op in failed_ops
            if op.action == "error"
            and any(
                marker in op.detail
                for marker in (
                    "SEARCH text not found",
                    "SEARCH text appears",
                    "no SEARCH/REPLACE hunks found",
                )
            )
        ]
        if not retryable_errors:
            return [], ""

        snippets: list[str] = []
        retry_paths: list[str] = []
        for op in retryable_errors:
            target, rel_path, path_err = self._resolve_workspace_path(op.path)
            if path_err or target is None or rel_path is None:
                continue
            if not target.exists():
                continue
            try:
                content = target.read_text(encoding="utf-8")
            except Exception:
                continue

            # Keep retry prompt bounded.
            max_chars = 9000
            shown = content[:max_chars]
            if len(content) > max_chars:
                shown += "\n... [truncated]"

            snippets.append(f"### {rel_path}\n```text\n{shown}\n```")
            retry_paths.append(rel_path)

        if not snippets:
            return [], ""

        retry_system = (
            "You are a precise code editor. "
            "Return ONLY edit blocks in this exact format:\n"
            "```edit:path/to/file.ext\n"
            "<<<<<<< SEARCH\n"
            "exact old text\n"
            "=======\n"
            "new text\n"
            ">>>>>>> REPLACE\n"
            "```\n"
            "Do not include prose."
        )
        retry_user = (
            "The previous edit failed because SEARCH text did not match exactly.\n\n"
            f"Original user request:\n{user_text}\n\n"
            "Previous model response:\n"
            f"{original_model_response}\n\n"
            "Current file contents:\n"
            f"{'\n\n'.join(snippets)}\n\n"
            "Generate corrected edit blocks that apply exactly to these files. "
            "If no change is needed, reply exactly: NO_CHANGES"
        )

        try:
            retry_response = await self.llm.chat(
                [{"role": "user", "content": retry_user}],
                system_prompt=retry_system,
            )
        except Exception as e:
            log.error(f"Retry edit call failed: {e}")
            return [], ""

        if not retry_response or retry_response.strip().upper() == "NO_CHANGES":
            return [], ""

        retry_ops, retry_cleaned = await self._process_file_blocks(retry_response)

        if retry_ops:
            ok_count = sum(1 for op in retry_ops if op.action != "error")
            err_count = sum(1 for op in retry_ops if op.action == "error")
            log.info(
                f"Retry edit result for {', '.join(retry_paths)}: "
                f"{ok_count} succeeded, {err_count} failed"
            )

        return retry_ops, retry_cleaned

    # ── /show ─────────────────────────────────────────────────

    async def cmd_show(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        session_id = str(update.effective_chat.id) if update.effective_chat else "?"
        self._log_user_message(session_id, "/show")

        uptime = int(time.time() - self.start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)

        stats = self.memory.stats()
        summary_status = "✅" if session_id in self._session_summaries else "—"
        active_skills = self.skills.active_records(session_id)
        installed_skills = self.skills.list_skills()

        voice_status = "✅ Groq Whisper" if self.config.groq_api_key else "❌ No GROQ_API_KEY"

        await self._reply_logged(
            update,
            f"🦞 <b>LightClaw Status</b>\n\n"
            f"<b>Provider:</b> {_escape_html(self.config.llm_provider)}\n"
            f"<b>Model:</b> {_escape_html(self.config.llm_model)}\n"
            f"<b>Context window:</b> {self.config.context_window:,} tokens\n"
            f"<b>Uptime:</b> {hours}h {minutes}m {seconds}s\n"
            f"<b>Memory:</b> {stats['total_interactions']} interactions\n"
            f"<b>Session summary:</b> {summary_status}\n"
            f"<b>Skills:</b> {len(active_skills)} active / {len(installed_skills)} installed\n"
            f"<b>Voice:</b> {voice_status}",
            parse_mode=ParseMode.HTML,
        )

    # ── Voice Message Handler ─────────────────────────────────

    async def handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle voice messages — download, transcribe, then process as text."""
        if not update.effective_user or not update.message or not update.message.voice:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        voice = update.message.voice
        chat_id = update.effective_chat.id if update.effective_chat else 0

        # Send typing indicator immediately
        if update.effective_chat:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        # Download voice file
        try:
            voice_file = await voice.get_file()
            voice_bytes = await voice_file.download_as_bytearray()
        except Exception as e:
            log.error(f"Failed to download voice: {e}")
            await self._reply_logged(update, "⚠️ Couldn't download voice message.")
            return

        # Transcribe
        text = await transcribe_voice(bytes(voice_bytes), self.config.groq_api_key)

        if text:
            caption = update.message.caption or ""
            user_text = f"[voice transcription: {text}]"
            if caption:
                user_text = f"{caption}\n{user_text}"
            log.info(f"Voice transcribed: {text[:80]}")
        else:
            user_text = "[voice message received — transcription not available]"
            if update.message.caption:
                user_text = f"{update.message.caption}\n{user_text}"

        # Process through the normal agent loop
        await self._process_user_message(update, context, user_text)

    # ── Photo Handler ─────────────────────────────────────────

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle photo messages — note the image and process caption."""
        if not update.effective_user or not update.message or not update.message.photo:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        caption = update.message.caption or ""
        user_text = f"[image: photo attached]\n{caption}" if caption else "[image: photo attached]"

        await self._process_user_message(update, context, user_text)

    # ── Document Handler ──────────────────────────────────────

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle document messages."""
        if not update.effective_user or not update.message or not update.message.document:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        doc = update.message.document
        filename = doc.file_name or "unknown file"
        caption = update.message.caption or ""
        user_text = f"[document: {filename}]\n{caption}" if caption else f"[document: {filename}]"

        await self._process_user_message(update, context, user_text)

    # ── Message Handler (the core loop) ───────────────────────

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages — the main conversational agent loop."""
        if not update.effective_user or not update.message or not update.message.text:
            return
        if not self.is_allowed(update.effective_user.id):
            return

        await self._process_user_message(update, context, update.message.text)

    # ── Core Processing Pipeline ──────────────────────────────

    async def _process_user_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str
    ):
        """
        Core agent loop:
        1. Send "Thinking… 💭" placeholder
        2. Recall relevant memories (RAG)
        3. Get recent conversation history + clean orphans
        4. Build system prompt with personality + memories + summary
        5. Send to LLM (with retry on context overflow)
        6. Ingest user message into memory
        7. Apply file create/edit operations from model response
        8. Ingest cleaned assistant response into memory
        9. Edit placeholder with final response
        10. Trigger async summarization if needed
        """
        chat_id = update.effective_chat.id if update.effective_chat else 0
        session_id = str(chat_id)

        self._log_user_message(session_id, user_text)

        # 1. Send typing + placeholder
        placeholder = None
        try:
            if update.effective_chat:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            self._log_bot_message(session_id, "Thinking... 💭")
            placeholder = await update.message.reply_text("Thinking... 💭")
        except Exception:
            pass

        # 2. Recall relevant memories
        memories = self.memory.recall(user_text, top_k=self.config.memory_top_k)
        memories_text = self.memory.format_memories_for_prompt(memories)

        # 3. Get recent conversation history + clean orphans
        recent = self.memory.get_recent(session_id, limit=20)
        recent = self._clean_orphan_messages(recent)

        # 4. Get session summary
        summary = self._get_session_summary(session_id)
        skills_text = await asyncio.to_thread(self.skills.prompt_context, session_id)

        # 5. Build system prompt with personality
        system_prompt = build_system_prompt(
            self.config, self.personality, memories_text, summary, skills_text
        )

        # 6. Build messages for LLM
        messages = list(recent)
        messages.append({"role": "user", "content": user_text})

        # 7. Call LLM (with retry on context overflow)
        start_time_mono = time.monotonic()
        response = None
        max_retries = 2

        for retry in range(max_retries + 1):
            try:
                response = await self.llm.chat(messages, system_prompt)
                break
            except Exception as e:
                if retry < max_retries and self._is_context_error(str(e)):
                    log.warning(f"Context overflow detected, compressing history (retry {retry + 1})")
                    # Emergency compression: drop oldest 50%
                    if len(messages) > 4:
                        mid = len(messages) // 2
                        messages = (
                            messages[:1]
                            + [{"role": "system", "content": f"[Emergency: dropped {mid} oldest messages due to context limit]"}]
                            + messages[mid:]
                        )
                    continue
                log.error(f"LLM call failed: {e}")
                response = f"⚠️ Error communicating with {self.config.llm_provider}: {e}"
                break

        if response is None:
            response = "⚠️ Failed to get a response after retries. Please try again."

        elapsed = time.monotonic() - start_time_mono
        log.info(f"[{session_id}] LLM response ({elapsed:.1f}s)")

        # 8. Ingest into memory
        self.memory.ingest("user", user_text, session_id)

        # 9. Apply file operations (create/edit) and clean the response
        file_ops, cleaned_response = await self._process_file_blocks(response)
        failed_ops = [op for op in file_ops if op.action == "error"]
        if failed_ops:
            retry_ops, retry_cleaned = await self._retry_failed_edits(
                user_text=user_text,
                original_model_response=response,
                failed_ops=failed_ops,
            )
            if retry_ops:
                recovered_paths = {op.path for op in retry_ops if op.action != "error"}
                if recovered_paths:
                    file_ops = [
                        op for op in file_ops
                        if not (op.action == "error" and op.path in recovered_paths)
                    ]
                    if retry_cleaned:
                        cleaned_response = "\n\n".join(
                            part for part in [cleaned_response, retry_cleaned] if part
                        ).strip()
                file_ops.extend(retry_ops)

        # 10. Build final message (short text + file operation summary)
        workspace_label = self._workspace_display_path()
        visible_response = cleaned_response
        if file_ops:
            success_count = sum(1 for op in file_ops if op.action != "error")
            if success_count > 0:
                visible_response = "Done. Saved requested changes to files."
            else:
                visible_response = self._compact_response_for_file_ops(cleaned_response)

        response_parts = [visible_response] if visible_response else []
        if file_ops:
            response_parts.append(
                self._render_file_operations(
                    file_ops,
                    include_diffs=True,
                    workspace_label=workspace_label,
                )
            )
        final_markdown_response = "\n\n".join(part for part in response_parts if part).strip()
        if self._is_large_code_leak(final_markdown_response):
            # Hard guardrail: never send giant code dumps to Telegram.
            if file_ops:
                final_markdown_response = (
                    "Done. Saved requested changes to files.\n\n"
                    + self._render_file_operations(
                        file_ops,
                        include_diffs=True,
                        workspace_label=workspace_label,
                    )
                )
            else:
                final_markdown_response = (
                    "Large code output was suppressed.\n"
                    "Please ask again and include explicit file names (e.g. ```html:index.html ...```)."
                )
        if not final_markdown_response:
            final_markdown_response = "Done."

        # Ingest a compact version into memory (without long diff blocks)
        memory_text = visible_response if file_ops else cleaned_response
        memory_parts = [memory_text] if memory_text else []
        if file_ops:
            memory_parts.append(
                self._render_file_operations(
                    file_ops,
                    include_diffs=False,
                    workspace_label=workspace_label,
                )
            )
        memory_response = "\n\n".join(part for part in memory_parts if part).strip() or "Done."
        self.memory.ingest("assistant", memory_response, session_id)

        # 11. Edit placeholder with final response
        await self._send_response(placeholder, update, final_markdown_response)

        # 12. Async summarization check
        asyncio.create_task(self.maybe_summarize(session_id))

    # ── Message Chunking (Telegram 4096 char limit) ─────────

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


# ──────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────


def main():
    """Start the LightClaw Telegram bot."""
    config = load_config()

    # Resolve runtime paths relative to LIGHTCLAW_HOME (if set) or project root.
    config.workspace_path = str(resolve_runtime_path(config.workspace_path))
    config.memory_db_path = str(resolve_runtime_path(config.memory_db_path))
    config.skills_state_path = str(resolve_runtime_path(config.skills_state_path))

    # Ensure workspace directory exists
    workspace = Path(config.workspace_path)
    workspace.mkdir(parents=True, exist_ok=True)

    # Validate required config
    if not config.telegram_bot_token:
        log.error("TELEGRAM_BOT_TOKEN is required. Set it in .env")
        return

    if not config.llm_provider:
        log.error(
            "No LLM provider configured. Set LLM_PROVIDER and the corresponding API key in .env"
        )
        return

    log.info("🦞 LightClaw starting...")
    log.info(f"   Provider: {config.llm_provider} ({config.llm_model})")
    log.info(f"   Memory DB: {config.memory_db_path}")
    log.info(f"   Workspace: {config.workspace_path}")
    log.info(f"   Skills state: {config.skills_state_path}")
    log.info(f"   Skills hub: {config.skills_hub_base_url}")
    log.info(f"   Context window: {config.context_window:,} tokens")
    if config.groq_api_key:
        log.info("   Voice: ✅ Groq Whisper enabled")
    else:
        log.info("   Voice: ❌ disabled (set GROQ_API_KEY)")
    if config.telegram_allowed_users:
        log.info(f"   Allowed users: {', '.join(config.telegram_allowed_users)}")
    else:
        log.info("   Allowed users: everyone")

    bot = LightClawBot(config)

    # Print memory stats
    stats = bot.memory.stats()
    skill_count = len(bot.skills.list_skills())
    log.info(
        f"   Memory: {stats['total_interactions']} interactions, "
        f"{stats['unique_sessions']} sessions, "
        f"{stats['vocabulary_size']} vocabulary terms"
    )
    log.info(f"   Skills: {skill_count} installed")

    # Print personality source
    ws = Path(config.workspace_path)
    loaded = [f for f in ["IDENTITY.md", "SOUL.md", "USER.md"] if (ws / f).exists()]
    if loaded:
        log.info(f"   Personality: {', '.join(loaded)}")
    else:
        log.info("   Personality: built-in default")

    # Build Telegram application
    app = Application.builder().token(config.telegram_bot_token).build()

    # Register handlers
    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CommandHandler("clear", bot.cmd_clear))
    app.add_handler(CommandHandler("wipe_memory", bot.cmd_wipe_memory))
    app.add_handler(CommandHandler("wipe", bot.cmd_wipe_memory))
    app.add_handler(CommandHandler("memory", bot.cmd_memory))
    app.add_handler(CommandHandler("recall", bot.cmd_recall))
    app.add_handler(CommandHandler("skills", bot.cmd_skills))
    app.add_handler(CommandHandler("show", bot.cmd_show))
    app.add_handler(MessageHandler(filters.VOICE, bot.handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, bot.handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, bot.handle_document))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message)
    )
    app.add_error_handler(bot.on_error)

    log.info("🦞 LightClaw is running! Press Ctrl+C to stop.")

    # Start polling
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
