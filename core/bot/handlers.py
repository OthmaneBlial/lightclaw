"""Telegram media/text handlers and main user-message processing."""

from __future__ import annotations

import asyncio
import io
import time

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from ..logging_setup import log
from ..markdown import _escape_html
from ..personality import build_system_prompt
from ..voice import transcribe_voice


class BotHandlersMixin:
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
        active_agent = self._agent_mode_by_session.get(session_id, "none")

        voice_status = "✅ Groq Whisper" if self.config.groq_api_key else "❌ No GROQ_API_KEY"

        await self._reply_logged(
            update,
            f"🦞 <b>LightClaw Status</b>\n\n"
            f"<b>Provider:</b> {_escape_html(self.config.llm_provider)}\n"
            f"<b>Model:</b> {_escape_html(self.config.llm_model)}\n"
            f"<b>Context window:</b> {self.config.context_window:,} tokens\n"
            f"<b>Max output:</b> {self.config.max_output_tokens:,} tokens\n"
            f"<b>Uptime:</b> {hours}h {minutes}m {seconds}s\n"
            f"<b>Memory:</b> {stats['total_interactions']} interactions\n"
            f"<b>Session summary:</b> {summary_status}\n"
            f"<b>Skills:</b> {len(active_skills)} active / {len(installed_skills)} installed\n"
            f"<b>Delegation:</b> {_escape_html(active_agent)}\n"
            f"<b>Delegation safety:</b> {_escape_html(self.config.local_agent_safety_mode)}\n"
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

        # Optional delegation mode: route normal messages to local coding agent.
        active_agent = self._agent_mode_by_session.get(session_id)
        if active_agent:
            log.info(
                f"[{session_id}] Delegation mode active ({active_agent}); routing message to local agent"
            )
            self.memory.ingest("user", user_text, session_id)
            delegated_response = await self._run_local_agent_task(
                session_id=session_id,
                agent=active_agent,
                task=user_text,
            )
            self.memory.ingest("assistant", delegated_response, session_id)
            await self._send_response(placeholder, update, delegated_response)
            if not self._llm_backoff_active():
                asyncio.create_task(self.maybe_summarize(session_id))
            return

        # Provider backoff: avoid hammering the API on every user message.
        if self._llm_backoff_active():
            remaining = self._llm_backoff_remaining_sec()
            wait_hint = f"{remaining}s" if remaining > 0 else "a short while"
            self.memory.ingest("user", user_text, session_id)
            quick_reply = (
                f"⚠️ {self.config.llm_provider} is temporarily unavailable "
                "(quota/billing or rate limit).\n"
                f"Please retry in about {wait_hint}, or top up your provider balance."
            )
            await self._send_response(placeholder, update, quick_reply)
            return

        # 2. Recall relevant memories
        memories = self.memory.recall(user_text, top_k=self.config.memory_top_k)
        memories = self._filter_recalled_memories(memories)
        memories_text = self.memory.format_memories_for_prompt(memories)

        # 3. Get recent conversation history + clean orphans
        recent = self.memory.get_recent(session_id, limit=20)
        recent = self._clean_orphan_messages(recent)
        recent = self._filter_recent_context(recent)

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
        provider_error_response = self._is_provider_error_text(response)
        if provider_error_response:
            self._set_llm_backoff()
        else:
            self._clear_llm_backoff()

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

        # 9b. Force a second pass when the model returned no-op prose for file tasks.
        success_ops = [op for op in file_ops if op.action != "error"]
        if not success_ops and (
            self._is_file_intent(user_text)
            or self._is_deferral_response(response)
            or self._is_deferral_response(cleaned_response)
        ):
            forced_ops, forced_cleaned = await self._force_file_ops_pass(
                session_id=session_id,
                user_text=user_text,
                prior_model_response=response,
            )
            if forced_ops:
                recovered_paths = {op.path for op in forced_ops if op.action != "error"}
                if recovered_paths:
                    file_ops = [op for op in file_ops if op.path not in recovered_paths]
                file_ops.extend(forced_ops)
                if forced_cleaned:
                    cleaned_response = "\n\n".join(
                        part for part in [cleaned_response, forced_cleaned] if part
                    ).strip()

        # 9c. Repair likely-truncated HTML outputs before user-facing response.
        repair_ops = await self._repair_incomplete_html(session_id, user_text, file_ops)
        if repair_ops:
            repaired_paths = {op.path for op in repair_ops if op.action != "error"}
            if repaired_paths:
                file_ops = [op for op in file_ops if op.path not in repaired_paths]
            file_ops.extend(repair_ops)

        # Track last touched file to support follow-up edit requests like "add more".
        success_ops = [op for op in file_ops if op.action != "error" and op.path]
        if success_ops:
            self._last_file_by_session[session_id] = success_ops[-1].path

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
        if not provider_error_response:
            asyncio.create_task(self.maybe_summarize(session_id))

    # ── Message Chunking (Telegram 4096 char limit) ─────────

