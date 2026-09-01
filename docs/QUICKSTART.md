# Five-Minute Token-Free Quickstart

This path proves LightClaw's complete request-to-review loop without Telegram, a model,
credentials, network calls, or an existing repository. Times are guidance, not a guarantee.

## Minute 0–1: clone and isolate

Use macOS or Ubuntu/Linux with Python 3.10–3.13 and Git:

```bash
git clone https://github.com/OthmaneBlial/lightclaw.git
cd lightclaw
python3 -m venv .venv
. .venv/bin/activate
```

## Minute 1–2: install the checkout

```bash
python -m pip install -e .
lightclaw --help
```

This installs the base runtime in the virtual environment; it does not download a vendor
SDK, configure a Telegram bot or provider, or write into global Python. A real provider is
added later with one of the documented `openai`, `claude`, or `gemini` extras.

## Minute 2–3: run the complete fixture

```bash
lightclaw demo --scenario repo-task --output ./lightclaw-demo --json
```

The fixture replays a recorded phone request and approval, creates a disposable Git-backed
Python service, adds a health check, runs a real unit test, and creates review evidence.

## Minute 3–4: inspect proof before claims

```bash
python -m json.tool lightclaw-demo/receipt.json | head -80
sed -n '1,160p' lightclaw-demo/review/changes.patch
sed -n '1,120p' lightclaw-demo/artifact/test-output.txt
```

You should see an accepted receipt, an actual patch, and a passing `unittest` exit result.
The demo makes no claim about live provider quality or Telegram connectivity.

## Minute 4–5: choose the next bounded step

- Replay [persistent memory](../showcase/entries/persistent-memory/).
- Replay the [multi-agent failure/repair audit](../showcase/entries/audited-multi-agent/).
- Read the [security boundary](THREAT_MODEL.md) before connecting private work.
- When ready for a real local install, follow [onboarding and rollback](INSTALL.md).

Delete `lightclaw-demo/` when finished. The command does not publish or upload it.
