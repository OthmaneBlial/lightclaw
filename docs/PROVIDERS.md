# Provider Contract

LightClaw supports six named adapters through one typed internal protocol. Provider-specific SDK objects never cross that boundary.

## Install only the provider you use

The deterministic demo needs only the base package. Live providers are optional so a first
install does not download three unrelated vendor SDK families:

```bash
python -m pip install 'lightclaw-ai[openai] @ git+https://github.com/OthmaneBlial/lightclaw.git'
# or use [claude], [gemini], or [providers]
```

The `openai` extra covers OpenAI plus the xAI, DeepSeek, and Z-AI compatible transports.
The `providers` extra installs every SDK family. `lightclaw doctor` verifies the configured
provider SDK without exposing credentials, and client construction fails with the exact
missing extra instead of an unscoped import traceback.

## Normalized response

`LLMClient.complete()` returns:

- provider and model identity;
- text, preserving an empty response instead of inventing content;
- nullable input, output, total, cache, reasoning, and tool token counts;
- bounded attempt count and adapter latency.

`LLMClient.chat()` remains the compatibility text facade used by existing Telegram and terminal handlers. New internal integrations should use `complete()` when usage or structured errors matter.

All adapters receive the same `ProviderRequest` with user/assistant messages, system prompt, output budget, and timeout. SDK retries are disabled. LightClaw applies one central exponential retry policy only to rate limits, timeouts, network failures, HTTP 408/409, and provider 5xx responses. Authentication, quota, invalid-request, and invalid-response failures do not retry.

Provider-reported timeouts can retry after the attempt ends. A LightClaw deadline timeout is not retried because a synchronous SDK request may continue after its coroutine is canceled.

Configure the bounded policy with:

```dotenv
PROVIDER_TIMEOUT_SEC=60
PROVIDER_MAX_RETRIES=2
```

The retry value is retries after the first attempt. SDK clients are closed explicitly during LightClaw shutdown.

Closing a client prevents another retry after backoff and rejects queued SDK/HTTP work that has not started yet. It does not forcibly stop an operation already running in a synchronous SDK thread; that work still depends on the transport timeout.

Custom Anthropic-compatible endpoints set with `ANTHROPIC_BASE_URL` receive direct
requests. Redirects are rejected so the API key cannot be forwarded to another host.

## Compatibility evidence

The [generated compatibility matrix](generated/provider-compatibility.md) comes from the
provider registry plus six versioned response fixtures. The canonical local quality suite
runs the shared contract tests and rejects matrix drift; the GitHub CI workflow is disabled.
Fixture success proves deterministic request/response mapping, normalized usage,
retry/error behavior, and lifecycle handling. It does not prove live API availability,
latency, cost, or model quality.

xAI, DeepSeek, and Z.AI use their own provider identity and endpoint through an OpenAI-compatible transport. They are not represented as OpenAI-operated or OpenAI-certified services.

DeepSeek's current Chat Completions model IDs are `deepseek-flash` and `deepseek-v4-pro`.
LightClaw uses `deepseek-flash` for new defaults and maps retired `deepseek-chat` and
`deepseek-reasoner` settings to that ID. See the [official model list](https://api-docs.deepseek.com/api/create-chat-completion/).

## Adding a provider

A new provider is accepted only when the same change includes:

1. a registry entry with an accountable maintainer;
2. a bounded adapter implementing `ProviderAdapter`;
3. a recorded, secret-free response fixture;
4. the shared contract tests for text, usage, timeout, retry, errors, and close;
5. regenerated JSON and Markdown compatibility outputs;
6. lifecycle evidence from official vendor documentation.

No adapter may introduce its own unbounded retry loop or return a vendor exception as its public contract.

## Manual live verification

Use `python scripts/provider_smoke_test.py --providers <names>` only with disposable credentials. The script reports normalized nullable usage and attempt count and closes every client. Keep live results private unless fully sanitized; the public matrix remains token-free.
