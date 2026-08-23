# LightClaw

**Turn a Telegram request into reviewed, verified work on your local projects.**

[![CI](https://github.com/OthmaneBlial/lightclaw/actions/workflows/ci.yml/badge.svg)](https://github.com/OthmaneBlial/lightclaw/actions/workflows/ci.yml)
[![CodeQL](https://github.com/OthmaneBlial/lightclaw/actions/workflows/codeql.yml/badge.svg)](https://github.com/OthmaneBlial/lightclaw/actions/workflows/codeql.yml)
[![Python 3.10–3.13](https://img.shields.io/badge/python-3.10%E2%80%933.13-58c7ff)](pyproject.toml)
[![MIT](https://img.shields.io/badge/license-MIT-72f1b8)](LICENSE)

LightClaw is an auditable Telegram mission control for local Codex and Claude coding agents. You send a goal, review the scoped DAG, watch bounded workers, and receive the diff, checks, artifacts, cost/time evidence, and recovery instructions.

![Silent 24-second LightClaw walkthrough: request, plan, workers, tests, and run receipt](assets/demo.svg)

[Watch the demo](#see-it-work-without-a-token) · [Install safely](docs/INSTALL.md) · [Read the security boundary](docs/THREAT_MODEL.md)

> Alpha software with meaningful host access. Telegram access fails closed, delegated workers do not inherit provider secrets, and trusted host execution always requires per-run confirmation.

## One complete story

1. From Telegram: “Add a health check, test it, and return only verified work.”
2. LightClaw shows `builder → verifier`, requested paths, risk, and capability before execution.
3. You approve, edit, or cancel the plan.
4. Workers run in a dedicated task directory with a minimal environment.
5. LightClaw returns actual test evidence, a reviewable Git patch/branch, file hashes, a private JSON/Markdown receipt, and a scoped undo path.

That loop—not a long channel list—is the product.

## See it work without a token

The deterministic demo contacts no model and needs no Telegram account:

```bash
git clone https://github.com/OthmaneBlial/lightclaw.git
cd lightclaw
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e .
lightclaw demo
```

It replays a recorded phone request and approval, creates a tiny Git-backed Python service, runs a real unit test, and finishes with `changes.patch`, `artifact.json`, `receipt.json`, and `receipt.md`. Nothing is pushed. Try every product story:

```bash
lightclaw demo --scenario memory
lightclaw demo --scenario repo-task
lightclaw demo --scenario multi-agent
```

The exact prompts, outputs, cost boundaries, cleanup, and limitations live in:

- [Telegram memory](examples/telegram-memory/README.md)
- [Telegram repository task](examples/telegram-repo-task/README.md)
- [Telegram multi-agent plan](examples/telegram-multi-agent/README.md)

## LightClaw is / is not

| LightClaw is | LightClaw is not |
|---|---|
| A Telegram-first review and control surface for local coding agents | A hosted multi-tenant agent service |
| Local SQLite memory, task workspaces, receipts, and skills | Fully local inference when a hosted model is configured |
| Human approval around plans and trusted execution | A guarantee that model-generated changes are correct |
| A small Python product with optional edges | A feature-for-feature OpenClaw clone |
| Auditable fixture stories that run for $0 | Proof of real-provider latency, cost, or availability |

## Safe installation

Use an isolated tool environment:

```bash
pipx install 'git+https://github.com/OthmaneBlial/lightclaw.git'
# or
uv tool install 'git+https://github.com/OthmaneBlial/lightclaw.git'
```

Then:

```bash
lightclaw demo
lightclaw onboard --configure
lightclaw doctor
lightclaw run
```

The supported paths are app-specific:

- config: `~/.config/lightclaw/config.env` (mode `0600`);
- memory, skills, logs, and receipts: `~/.lightclaw/`;
- owned task workspaces: `~/.lightclaw/workspace/`.

Existing configuration is backed up before reset. The compatibility installer uses its own virtual environment and never installs into global Python. See [install, upgrade, undo, and uninstall](docs/INSTALL.md).

## Signature workflow

```text
Telegram text or voice goal
  -> scoped DAG with risk, paths, and expected outputs
  -> approve / edit / deny
  -> isolated Codex or Claude workers
  -> compact live progress
  -> tests + Git patch + file evidence + receipt
  -> accept / reject / selective apply / optional PR preview
```

Current capabilities include:

- fail-closed Telegram allowlisting and privileged-command rate limits;
- OpenAI, xAI, Anthropic, Gemini, DeepSeek, and Z-AI routing;
- Codex and Claude delegation profiles: `observe`, `workspace-write`, `trusted-command`;
- DAG planning, owned paths, JSON handoffs, acceptance checks, and bounded repair;
- namespaced SQLite FTS5 lexical recall with retention, export, and selective delete;
- permission-manifest skills with pinned provenance, hash review, and prompt-only activation;
- voice transcription, scheduled jobs, heartbeat, Telegram, and terminal chat;
- token-free fixture adapters used by the full CI matrix.

## Security model

An empty `TELEGRAM_ALLOWED_USERS` blocks startup. Intentionally public bots require `LIGHTCLAW_PUBLIC_BOT_ACK=yes`. Delegated processes get a minimal environment that excludes Telegram/provider keys. `lightclaw undo` refuses paths that lack a LightClaw ownership record.

These controls do not protect the host after you explicitly enable `trusted-command`, install malicious instructions, weaken an external CLI sandbox, or place secrets inside a readable task workspace.

Read [SECURITY.md](SECURITY.md) and the [threat model](docs/THREAT_MODEL.md). Report vulnerabilities through [private vulnerability reporting](https://github.com/OthmaneBlial/lightclaw/security/advisories/new), never a public issue.

## Documentation

- [Install, upgrade, undo, uninstall](docs/INSTALL.md)
- [Threat model](docs/THREAT_MODEL.md)
- [Real Telegram verification](docs/MANUAL_VERIFICATION.md)
- [Run receipts and sanitized Run Cards](docs/RUN_RECEIPTS.md)
- [Durable queues, cancellation, and recovery](docs/JOB_CONTROL.md)
- [Telegram approvals and high-risk confirmation](docs/APPROVALS.md)
- [Reviewable patches, selective apply, and optional PRs](docs/ARTIFACTS.md)
- [Namespaced lexical memory, retention, export, and evaluation](docs/MEMORY.md)
- [Build and validate a safe skill in 10 minutes](docs/SAFE_SKILLS.md)
- [Typed provider contract and generated compatibility matrix](docs/PROVIDERS.md)
- [Architecture map, enforced budgets, and decision records](docs/ARCHITECTURE.md)
- [Upgrade and rollback policy](docs/UPGRADING.md)
- [Optional systemd service](docs/SYSTEMD.md)
- [Reproducible benchmarks](bench/README.md)
- [Multi-agent guide](MULTI_AGENT.md)
- [Roadmap](ROADMAP.md)
- [Security policy](SECURITY.md)

## Honest alpha limits

- Memory retrieval is bounded SQLite FTS5 lexical search, not semantic understanding. Embeddings are an optional adapter and are never required.
- Fixture demos prove LightClaw contracts, not external model quality.
- External coding-agent CLIs remain separate security boundaries with their own versions and settings.
- The package is installable from Git today; the first stable PyPI release follows the release gate in the roadmap.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python scripts/quality.py
```

This is the canonical local quality command. CI additionally runs the key-free suite on
Python 3.10–3.13 across Ubuntu and macOS, installs the wheel in a clean environment,
audits dependencies, and runs all three deterministic product stories. Read
[CONTRIBUTING.md](CONTRIBUTING.md), the [support routes](SUPPORT.md), and the
[Code of Conduct](CODE_OF_CONDUCT.md) before participating.

Licensed under [MIT](LICENSE).
