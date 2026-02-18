"""
LightClaw — Unified LLM Provider
Single class routing to OpenAI, xAI, Claude, Gemini, or Z-AI.
Unified LLM provider interface.
"""

import asyncio
import logging
from config import Config

log = logging.getLogger("lightclaw.providers")


class LLMClient:
    """
    Unified LLM interface. Routes to the correct SDK based on provider name.

    Supported providers:
      - openai  → OpenAI ChatGPT (via openai SDK)
      - xai     → xAI Grok (via openai SDK with custom base_url)
      - claude  → Anthropic Claude (via anthropic SDK)
      - gemini  → Google Gemini (via google-generativeai SDK)
      - zai     → Z-AI / Zhipu GLM (via openai SDK with custom base_url)
    """

    def __init__(self, config: Config):
        self.config = config
        self.provider_name = config.llm_provider
        self.model = config.llm_model
        self.max_output_tokens = max(512, int(getattr(config, "max_output_tokens", 4096) or 4096))
        self._client = None

        self._init_client()
        log.info(f"LLM output budget: {self.max_output_tokens} tokens")

    def _init_client(self):
        """Initialize the appropriate SDK client."""
        if self.provider_name in ("openai", "xai", "zai"):
            import openai

            if self.provider_name == "xai":
                if not self.config.xai_api_key:
                    raise ValueError("XAI_API_KEY is required when LLM_PROVIDER=xai")
                self._client = openai.OpenAI(
                    api_key=self.config.xai_api_key,
                    base_url="https://api.x.ai/v1",
                )
            elif self.provider_name == "zai":
                if not self.config.zai_api_key:
                    raise ValueError("ZAI_API_KEY is required when LLM_PROVIDER=zai")
                self._client = openai.OpenAI(
                    api_key=self.config.zai_api_key,
                    base_url="https://open.bigmodel.cn/api/paas/v4",
                )
            else:
                if not self.config.openai_api_key:
                    raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
                self._client = openai.OpenAI(
                    api_key=self.config.openai_api_key,
                )
            log.info(f"Initialized {self.provider_name} provider (model: {self.model})")

        elif self.provider_name == "claude":
            import anthropic

            if not self.config.anthropic_api_key:
                raise ValueError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=claude")
            self._client = anthropic.Anthropic(
                api_key=self.config.anthropic_api_key,
            )
            log.info(f"Initialized Claude provider (model: {self.model})")

        elif self.provider_name == "gemini":
            import google.generativeai as genai

            if not self.config.gemini_api_key:
                raise ValueError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini")
            genai.configure(api_key=self.config.gemini_api_key)
            self._client = genai.GenerativeModel(self.model)
            log.info(f"Initialized Gemini provider (model: {self.model})")

        else:
            raise ValueError(
                f"Unknown provider: {self.provider_name!r}. "
                f"Supported: openai, xai, claude, gemini, zai"
            )

    async def chat(
        self,
        messages: list[dict],
        system_prompt: str = "",
        max_output_tokens: int | None = None,
    ) -> str:
        """
        Send messages to the LLM and return the response as a plain string.

        Args:
            messages: List of {"role": "user"|"assistant", "content": "..."} dicts.
            system_prompt: System prompt injected at the beginning.
            max_output_tokens: Optional override for output token budget.

        Returns:
            The assistant's response text.
        """
        try:
            if self.provider_name in ("openai", "xai", "zai"):
                return await self._chat_openai(messages, system_prompt, max_output_tokens)
            elif self.provider_name == "claude":
                return await self._chat_claude(messages, system_prompt, max_output_tokens)
            elif self.provider_name == "gemini":
                return await self._chat_gemini(messages, system_prompt, max_output_tokens)
        except Exception as e:
            log.error(f"LLM call failed ({self.provider_name}): {e}")
            return f"⚠️ Error communicating with {self.provider_name}: {e}"

    # ── OpenAI / xAI ──────────────────────────────────────────

    async def _chat_openai(
        self,
        messages: list[dict],
        system_prompt: str,
        max_output_tokens: int | None = None,
    ) -> str:
        """Chat via OpenAI-compatible API (covers ChatGPT, xAI Grok, and Z-AI GLM)."""
        api_messages = []
        if system_prompt:
            api_messages.append({"role": "system", "content": system_prompt})
        api_messages.extend(messages)

        output_tokens = max(256, int(max_output_tokens or self.max_output_tokens))
        try:
            response = await asyncio.to_thread(
                self._client.chat.completions.create,
                model=self.model,
                messages=api_messages,
                max_tokens=output_tokens,
                temperature=0.7,
            )
        except Exception as e:
            err_text = str(e).lower()
            limit_related = any(
                marker in err_text
                for marker in (
                    "max_tokens",
                    "max token",
                    "max output",
                    "out of range",
                    "too large",
                    "exceed",
                    "greater than",
                    "must be less",
                )
            )
            if output_tokens > 4096 and limit_related:
                log.warning(
                    f"{self.provider_name} rejected max_tokens={output_tokens}; retrying with 4096"
                )
                response = await asyncio.to_thread(
                    self._client.chat.completions.create,
                    model=self.model,
                    messages=api_messages,
                    max_tokens=4096,
                    temperature=0.7,
                )
            else:
                raise
        return response.choices[0].message.content or ""

    # ── Claude ────────────────────────────────────────────────

    async def _chat_claude(
        self,
        messages: list[dict],
        system_prompt: str,
        max_output_tokens: int | None = None,
    ) -> str:
        """Chat via Anthropic's Messages API (system prompt is a separate param)."""
        # Claude requires alternating user/assistant messages
        # Filter out any system messages from the list
        api_messages = [m for m in messages if m.get("role") in ("user", "assistant")]

        # Ensure messages start with a user message
        if not api_messages or api_messages[0]["role"] != "user":
            api_messages.insert(0, {"role": "user", "content": "Hello!"})

        output_tokens = max(256, int(max_output_tokens or self.max_output_tokens))
        kwargs = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": output_tokens,
        }
        if system_prompt:
            kwargs["system"] = system_prompt

        try:
            response = await asyncio.to_thread(self._client.messages.create, **kwargs)
        except Exception as e:
            err_text = str(e).lower()
            if output_tokens > 4096 and "max_tokens" in err_text:
                log.warning(f"claude rejected max_tokens={output_tokens}; retrying with 4096")
                kwargs["max_tokens"] = 4096
                response = await asyncio.to_thread(self._client.messages.create, **kwargs)
            else:
                raise

        # Extract text from content blocks
        text_parts = []
        for block in response.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)
        return "\n".join(text_parts)

    # ── Gemini ────────────────────────────────────────────────

    async def _chat_gemini(
        self,
        messages: list[dict],
        system_prompt: str,
        max_output_tokens: int | None = None,
    ) -> str:
        """Chat via Google Gemini's GenerativeModel API."""
        import google.generativeai as genai

        # Rebuild model with system instruction if provided
        if system_prompt:
            model = genai.GenerativeModel(
                self.model,
                system_instruction=system_prompt,
            )
        else:
            model = self._client

        # Convert messages to Gemini's format
        gemini_history = []
        for msg in messages[:-1]:  # all but last (last is the current prompt)
            role = "user" if msg["role"] == "user" else "model"
            gemini_history.append({"role": role, "parts": [msg["content"]]})

        chat = model.start_chat(history=gemini_history)

        # Send the last message
        last_msg = messages[-1]["content"] if messages else "Hello!"
        response = await asyncio.to_thread(chat.send_message, last_msg)

        return response.text or ""
