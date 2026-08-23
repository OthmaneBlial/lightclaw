# Provider Contract

LightClaw supports six named adapters through one typed internal protocol. Provider-specific SDK objects never cross that boundary.

## Normalized response

`LLMClient.complete()` returns:

- provider and model identity;
- text, preserving an empty response instead of inventing content;
- nullable input, output, total, cache, reasoning, and tool token counts;
- bounded attempt count and adapter latency.

`LLMClient.chat()` remains the compatibility text facade used by existing Telegram and terminal handlers. New internal integrations should use `complete()` when usage or structured errors matter.

All adapters receive the same `ProviderRequest` with user/assistant messages, system prompt, output budget, and timeout. SDK retries are disabled. LightClaw applies one central exponential retry policy only to rate limits, timeouts, network failures, HTTP 408/409, and provider 5xx responses. Authentication, quota, invalid-request, and invalid-response failures do not retry.

Configure the bounded policy with:

```dotenv
PROVIDER_TIMEOUT_SEC=60
PROVIDER_MAX_RETRIES=2
```

The retry value is retries after the first attempt. SDK clients are closed explicitly during LightClaw shutdown.

## Compatibility evidence

The [generated compatibility matrix](generated/provider-compatibility.md) comes from the provider registry plus six versioned response fixtures. CI runs the shared contract suite and rejects matrix drift. Fixture success proves deterministic request/response mapping, normalized usage, retry/error behavior, and lifecycle handling. It does not prove live API availability, latency, cost, or model quality.

xAI, DeepSeek, and Z.AI use their own provider identity and endpoint through an OpenAI-compatible transport. They are not represented as OpenAI-operated or OpenAI-certified services.

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
