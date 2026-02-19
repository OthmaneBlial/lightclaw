"""Session summarization and prompt-context filtering."""

from __future__ import annotations

import asyncio
import os
import re

from ..logging_setup import log


class BotContextMixin:
    @staticmethod
    def estimate_tokens(messages: list[dict]) -> int:
        """Estimate token count using a 2.5 chars/token heuristic."""
        total_chars = sum(len(m.get("content", "")) for m in messages)
        return total_chars * 2 // 5

    # ── Session Summarization ────────────────────────────────

    async def maybe_summarize(self, session_id: str):
        """Trigger summarization if history is too long or token count too high."""
        if self._llm_backoff_active():
            return

        recent = self.memory.get_recent(session_id, limit=100)
        recent = self._filter_recent_context(recent)
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

        existing_summary = self._sanitize_summary_for_prompt(
            self._session_summaries.get(session_id, "")
        )
        if self._is_provider_error_text(existing_summary):
            existing_summary = ""
            self._session_summaries.pop(session_id, None)

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
            summary = self._sanitize_summary_for_prompt(summary)
            if summary and not self._is_provider_error_text(summary):
                self._session_summaries[session_id] = summary
                self._clear_llm_backoff()
                if os.getenv("LIGHTCLAW_CHAT_MODE", "").strip() == "1":
                    log.debug(f"[{session_id}] Summarized {len(valid)} messages → {len(summary)} chars")
                else:
                    log.info(f"[{session_id}] Summarized {len(valid)} messages → {len(summary)} chars")
            elif summary and self._is_provider_error_text(summary):
                self._set_llm_backoff()
                log.warning(f"[{session_id}] Skipped summary update due to provider error response")
        except Exception as e:
            log.error(f"Summarization failed: {e}")

    def _get_session_summary(self, session_id: str) -> str:
        """Get the stored summary for a session."""
        # First check in-memory cache
        if session_id in self._session_summaries:
            summary = self._sanitize_summary_for_prompt(self._session_summaries[session_id])
            if self._is_provider_error_text(summary):
                self._session_summaries.pop(session_id, None)
                return ""
            return summary
        # Fall back to memory store
        summary = self._sanitize_summary_for_prompt(self.memory.get_summary(session_id))
        if self._is_provider_error_text(summary):
            return ""
        return summary

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

    @staticmethod
    def _is_delegation_transcript_text(text: str) -> bool:
        """Detect local-agent transcript wrappers to keep them out of normal LLM context."""
        normalized = (text or "").strip()
        if not normalized:
            return False
        if normalized.startswith("🤖 Delegated to "):
            return True
        return (
            "No workspace file changes detected." in normalized
            and "Created/updated:" in normalized
        )

    def _filter_recent_context(self, messages: list[dict]) -> list[dict]:
        """Remove delegation transcripts and /agent command noise from recent history."""
        filtered: list[dict] = []
        for msg in messages:
            role = (msg.get("role") or "").strip()
            content = msg.get("content", "")
            if role == "assistant" and self._is_delegation_transcript_text(content):
                continue
            if role == "user" and content.strip().lower().startswith("/agent"):
                continue
            filtered.append(msg)
        return filtered

    def _filter_recalled_memories(self, memories: list) -> list:
        """Remove recalled snippets that can trigger fake delegation-style replies."""
        filtered = []
        for rec in memories:
            if rec.role == "assistant" and self._is_delegation_transcript_text(rec.content):
                continue
            if rec.role == "user" and rec.content.strip().lower().startswith("/agent"):
                continue
            filtered.append(rec)
        return filtered

    def _sanitize_summary_for_prompt(self, summary: str) -> str:
        """Strip delegation transcript artifacts from persistent session summaries."""
        if not summary:
            return ""
        if self._is_delegation_transcript_text(summary):
            return ""
        cleaned_lines: list[str] = []
        for line in summary.splitlines():
            stripped = line.strip()
            if (
                stripped.startswith("🤖 Delegated to ")
                or "No workspace file changes detected." in stripped
                or stripped.startswith("Created/updated:")
            ):
                continue
            cleaned_lines.append(line)
        return "\n".join(cleaned_lines).strip()

    # ── /start ────────────────────────────────────────────────
