# Provider SDK Lifecycle Audit

Snapshot: 2026-08-23. This audit used official vendor documentation and official SDK repositories only.

## Decisions

| Surface | Decision | Lifecycle evidence |
|---|---|---|
| OpenAI | Keep synchronous Chat Completions behind the protocol. The official SDK describes Responses as primary while Chat Completions remains supported. Disable SDK retries, set a bounded timeout, tolerate nullable text/usage, and call `close()`. | [Official OpenAI Python SDK](https://github.com/openai/openai-python#usage) |
| Anthropic | Keep synchronous Messages behind the protocol. Aggregate text blocks, include cache creation/read counts in effective input usage, disable SDK retries, set a bounded timeout, and call `close()`. | [Official Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python#getting-started) |
| Gemini | Replace deprecated `google-generativeai` with GA `google-genai`. Use `genai.Client().models.generate_content`, millisecond `HttpOptions`, one SDK attempt, nullable usage metadata, and explicit `close()`. | [Official library status](https://ai.google.dev/gemini-api/docs/libraries#legacy-libraries-and-migration), [migration guide](https://ai.google.dev/gemini-api/docs/migrate#python) |
| xAI | Keep an `xai` identity over its documented OpenAI-compatible endpoint. | [xAI quickstart](https://docs.x.ai/developers/quickstart) |
| DeepSeek | Keep a `deepseek` identity over its documented compatible endpoint and retain its explicit output ceiling. | [DeepSeek API documentation](https://api-docs.deepseek.com/) |
| Z.AI | Keep a `zai` identity over its current documented compatible endpoint; do not promise complete OpenAI parity. | [Z.AI OpenAI Python guide](https://docs.z.ai/guides/develop/openai/python) |

## Dependency policy

The package uses bounded major-version ranges for the three native SDK families and declares `httpx` directly because the explicit Anthropic-compatible base URL path imports it. Vendor SDK internals remain inside `core.llm.adapters`; the rest of LightClaw depends only on the typed protocol.

Re-run this audit before widening a major-version range, replacing an API surface, or adding a provider. A successful recorded fixture is not evidence that an SDK is still maintained.
