"""
LightClaw — Unified LLM Provider
Single class routing to OpenAI, xAI, Claude, Gemini, DeepSeek, or Z-AI.
Unified LLM provider interface.
"""

import asyncio
import logging
from config import Config

log = logging.getLogger("lightclaw.providers")
OFFICIAL_ANTHROPIC_BASE_URL = "https://api.anthropic.com"


class LLMClient:
    """
    Unified LLM interface. Routes to the correct SDK based on provider name.

    Supported providers:
      - openai  → OpenAI ChatGPT (via openai SDK)
      - xai     → xAI Grok (via openai SDK with custom base_url)
      - claude  → Anthropic Claude (via anthropic SDK)
      - gemini  → Google Gemini (via google-generativeai SDK)
      - deepseek → DeepSeek (via openai SDK with custom base_url)
      - zai     → Z-AI / Zhipu GLM (via openai SDK with custom base_url)
    """

    def __init__(self, config: Config):
        self.config = config
        self.provider_name = config.llm_provider
        self.model = config.llm_model
        self.max_output_tokens = max(512, int(getattr(config, "max_output_tokens", 4096) or 4096))
        if self.provider_name == "deepseek" and self.max_output_tokens > 4096:
            # DeepSeek's chat endpoint commonly rejects larger max_tokens values.
            self.max_output_tokens = 4096
        self._client = None
        self._claude_api_key = ""
        self._claude_auth_token = ""
        self._claude_base_url = OFFICIAL_ANTHROPIC_BASE_URL
        self._claude_custom_base = False

        self._init_client()
        log.info(f"LLM output budget: {self.max_output_tokens} tokens")

    def _init_client(self):
        """Initialize the appropriate SDK client."""
        if self.provider_name in ("openai", "xai", "deepseek", "zai"):
            import openai

            if self.provider_name == "xai":
                if not self.config.xai_api_key:
                    raise ValueError("XAI_API_KEY is required when LLM_PROVIDER=xai")
                self._client = openai.OpenAI(
                    api_key=self.config.xai_api_key,
                    base_url="https://api.x.ai/v1",
                    max_retries=0,
                )
            elif self.provider_name == "deepseek":
                if not self.config.deepseek_api_key:
                    raise ValueError("DEEPSEEK_API_KEY is required when LLM_PROVIDER=deepseek")
                self._client = openai.OpenAI(
                    api_key=self.config.deepseek_api_key,
                    base_url="https://api.deepseek.com",
                    max_retries=0,
                )
            elif self.provider_name == "zai":
                if not self.config.zai_api_key:
                    raise ValueError("ZAI_API_KEY is required when LLM_PROVIDER=zai")
                self._client = openai.OpenAI(
                    api_key=self.config.zai_api_key,
                    base_url="https://open.bigmodel.cn/api/paas/v4",
                    max_retries=0,
                )
            else:
                if not self.config.openai_api_key:
                    raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
                self._client = openai.OpenAI(
                    api_key=self.config.openai_api_key,
                    max_retries=0,
                )
            log.info(f"Initialized {self.provider_name} provider (model: {self.model})")

        elif self.provider_name == "claude":
            import anthropic

            api_key = (self.config.anthropic_api_key or "").strip()
            auth_token = (self.config.anthropic_auth_token or "").strip()
            base_url = (self.config.anthropic_base_url or "").strip()
            normalized_base = base_url.rstrip("/")

            if not api_key and not auth_token:
                raise ValueError(
                    "ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is required when LLM_PROVIDER=claude"
                )

            auth_mode = "api_key"
            kwargs: dict[str, object] = {"base_url": base_url}
            if auth_token:
                if api_key:
                    log.warning(
                        "Both ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN are set; "
                        "preferring ANTHROPIC_AUTH_TOKEN."
                    )
                kwargs["auth_token"] = auth_token
                auth_mode = "auth_token"
            else:
                kwargs["api_key"] = api_key

            self._claude_api_key = api_key
            self._claude_auth_token = auth_token
            self._claude_base_url = normalized_base
            self._claude_custom_base = normalized_base != OFFICIAL_ANTHROPIC_BASE_URL
            self._client = anthropic.Anthropic(**kwargs)
            log.info(f"Initialized Claude provider (model: {self.model})")
            log.info(f"Claude auth mode: {auth_mode}")

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
                f"Supported: openai, xai, claude, gemini, deepseek, zai"
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
            if self.provider_name in ("openai", "xai", "deepseek", "zai"):
                return await self._chat_openai(messages, system_prompt, max_output_tokens)
            elif self.provider_name == "claude":
                return await self._chat_claude(messages, system_prompt, max_output_tokens)
            elif self.provider_name == "gemini":
                return await self._chat_gemini(messages, system_prompt, max_output_tokens)
        except Exception as e:
            err_text = str(e)
            lower_err = err_text.lower()
            if self.provider_name == "zai" and (
                "1113" in err_text
                or "余额不足" in err_text
                or "无可用资源包" in err_text
            ):
                log.error(f"LLM call failed ({self.provider_name}): {e}")
                return (
                    "⚠️ Error communicating with zai: account balance/package is exhausted "
                    "(provider code 1113). Recharge your ZAI account or switch provider."
                )
            if "429" in lower_err and "too many requests" in lower_err:
                log.error(f"LLM call failed ({self.provider_name}): {e}")
                return (
                    f"⚠️ Error communicating with {self.provider_name}: rate limit hit (429). "
                    "Please retry in a moment."
                )
            log.error(f"LLM call failed ({self.provider_name}): {e}")
            return f"⚠️ Error communicating with {self.provider_name}: {e}"

    # ── OpenAI / xAI ──────────────────────────────────────────

    async def _chat_openai(
        self,
        messages: list[dict],
        system_prompt: str,
        max_output_tokens: int | None = None,
    ) -> str:
        """Chat via OpenAI-compatible API (ChatGPT/xAI/DeepSeek/Z-AI)."""
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

        if self._claude_custom_base:
            return await self._chat_claude_http_compat(
                messages=api_messages,
                system_prompt=system_prompt,
                max_tokens=output_tokens,
            )

        connection_markers = (
            "connection error",
            "timed out",
            "timeout",
            "network",
            "temporary failure",
            "name or service not known",
        )

        last_error: Exception | None = None
        response = None
        for attempt in range(3):
            try:
                response = await asyncio.to_thread(self._client.messages.create, **kwargs)
                break
            except Exception as e:
                last_error = e
                err_text = str(e).lower()
                if output_tokens > 4096 and "max_tokens" in err_text:
                    log.warning(f"claude rejected max_tokens={output_tokens}; retrying with 4096")
                    kwargs["max_tokens"] = 4096
                    output_tokens = 4096
                    continue

                if any(marker in err_text for marker in connection_markers) and attempt < 2:
                    await asyncio.sleep(0.35 * (attempt + 1))
                    continue
                break

        if response is None:
            raise last_error if last_error is not None else RuntimeError("Claude request failed")

        # Extract text from content blocks
        text_parts = []
        for block in response.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)
        return "\n".join(text_parts)

    async def _chat_claude_http_compat(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int,
    ) -> str:
        """Fallback for Anthropic-compatible proxies that intermittently fail with SDK transport."""
        import httpx

        url = f"{self._claude_base_url}/v1/messages"
        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if self._claude_auth_token:
            headers["authorization"] = f"Bearer {self._claude_auth_token}"
        elif self._claude_api_key:
            headers["x-api-key"] = self._claude_api_key
        else:
            raise RuntimeError("Missing Claude credentials for HTTP compatibility fallback.")

        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if system_prompt:
            payload["system"] = system_prompt

        def _post() -> tuple[int, str]:
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                resp = client.post(url, headers=headers, json=payload)
            return resp.status_code, resp.text

        status_code = 0
        body_text = ""
        for attempt in range(3):
            try:
                status_code, body_text = await asyncio.to_thread(_post)
                break
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(0.35 * (attempt + 1))
                    continue
                raise RuntimeError(f"Claude compatibility HTTP connection error: {e}") from e
        if status_code >= 400:
            detail = (body_text or "").strip().replace("\n", " ")[:240]
            raise RuntimeError(
                f"Claude compatibility HTTP error {status_code}: {detail or 'empty response'}"
            )

        try:
            import json

            data = json.loads(body_text)
        except Exception as e:
            raise RuntimeError(f"Claude compatibility HTTP parse error: {e}") from e

        content = data.get("content")
        if not isinstance(content, list):
            raise RuntimeError("Claude compatibility HTTP response missing content blocks.")
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "\n".join(parts)
        return ""

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
