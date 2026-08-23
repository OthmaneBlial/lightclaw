# Contributing to LightClaw

Thanks for your interest in contributing.

This project has a very specific direction. Please read this file before opening a PR.

LightClaw is currently an early-stage, solo-maintainer project with a small community.

## Project Sense

LightClaw is intentionally small.

The core goal is not to become a full platform. The core goal is to be a lightweight base you can fork and build your own thing on top of.

Design choices are deliberate:
- Telegram-first only.
- A small set of well-known LLM providers.
- Minimal commands and minimal moving parts.
- Keep runtime simple and understandable.

If you want a fully featured product with broad platform scope, use OpenClaw by Peter Steinberger.

## What We Value Most

In priority order:

1. Deleting code and simplifying behavior.
2. Fixing bugs and regressions.
3. Security hardening and risk reduction.
4. Reliability and stability improvements.
5. Documentation clarity.

PRs that remove complexity are strongly preferred over PRs that add features.

## What This Project Is Not Trying To Be

Please do not open PRs that push LightClaw toward feature sprawl.

Examples of out-of-scope directions:
- New channel integrations (WhatsApp, Slack, Discord, etc.).
- Large framework layers, plugin platforms, or architecture rewrites.
- Feature expansion that increases operational complexity without clear safety/reliability gains.

## PR Acceptance Philosophy

A good LightClaw PR usually does one of these:
- Removes code safely.
- Fixes something concrete and testable.
- Tightens security boundaries.
- Makes behavior simpler and more predictable.

A PR is less likely to be accepted if it:
- Adds broad new surface area.
- Introduces large abstractions for small problems.
- Increases maintenance burden without strong justification.

## Evidence Standard for Every PR

AI-assisted work is welcome. The standard is ownership and evidence, regardless of which
tools produced the first draft:

- You understand every changed line.
- You can explain why each change exists.
- You tested observable behavior and relevant failure paths.
- You can describe risks and tradeoffs clearly.

Unreviewed output, unverifiable claims, and changes the contributor cannot maintain will
not be accepted. The same standard applies to hand-written and AI-assisted work.

## Security Contributions Are Highly Welcome

Especially valuable:
- Path traversal and symlink escape fixes.
- Safer defaults and clearer guardrails.
- Credential/token handling improvements.
- Dependency and supply-chain risk reduction.
- Better failure-mode handling and safer error paths.

Please include a short threat/risk note in security PRs:
- What risk is reduced.
- What boundary is being protected.
- Any compatibility tradeoff.

## Practical PR Guidelines

- Keep PRs focused and small.
- Prefer one concern per PR.
- Include before/after behavior.
- Update docs when behavior changes.
- Avoid unrelated refactors.

Rule of thumb: if your PR adds more code than it removes, explain why the added complexity is necessary for safety, reliability, or correctness.

## Start Locally

Use Python 3.10–3.13, install the development dependencies, and run the one canonical
quality command before submitting:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python scripts/quality.py
```

It checks lint, generated provider/architecture contracts, runtime budgets, the safe-skill
fixture, the full test suite, and package construction. CI adds the supported OS/Python
matrix, coverage evidence, dependency review, and clean-wheel smoke tests.

Use the [architecture module map](docs/ARCHITECTURE.md#runtime-domains) to find the owner
of a behavior. Compatibility facades should stay small; put a change in its stable domain.
Open support questions in [Discussions](SUPPORT.md), vulnerabilities privately through
[SECURITY.md](SECURITY.md), and code changes through the structured issue and PR forms.
Showcase contributions must follow the [public evidence contract](showcase/README.md), use
a sanitized Run Card rather than a raw receipt, and pass the token-free replay validator.

## Final Note

This repository reflects a strict lightweight philosophy and a clear maintainer point of view.

If that philosophy matches what you want to build, contributions are welcome.
