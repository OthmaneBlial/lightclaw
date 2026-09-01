# LightClaw documentation

LightClaw is an alpha Telegram control surface for reviewed, verified work by local Codex
and Claude coding agents. Start with the token-free path, then read the trust boundary before
connecting a bot, provider, or real repository.

## Start

- [Five-minute token-free quickstart](QUICKSTART.md)
- [Install, upgrade, undo, and uninstall](INSTALL.md)
- [Real Telegram verification](MANUAL_VERIFICATION.md)
- [Provider installation and compatibility contract](PROVIDERS.md)

## Understand the delivery contract

- [Architecture map and enforced budgets](ARCHITECTURE.md)
- [Run receipts and sanitized Run Cards](RUN_RECEIPTS.md)
- [Telegram approvals and high-risk confirmation](APPROVALS.md)
- [Reviewable patches, selective apply, and optional PR previews](ARTIFACTS.md)
- [Durable queues, cancellation, and recovery](JOB_CONTROL.md)
- [Namespaced lexical memory](MEMORY.md)

## Trust boundaries

- [Threat model](THREAT_MODEL.md)
- [Privacy and public-evidence boundary](PRIVACY.md)
- [Security policy](../SECURITY.md)
- [Permissioned skill contract](SAFE_SKILLS.md)

## Operate and maintain

- [Optional systemd service](SYSTEMD.md)
- [Upgrade and rollback policy](UPGRADING.md)
- [Maintenance cadence and evidence policy](MAINTENANCE.md)
- [Maintainer publishing runbook](PUBLISHING.md)
- [Provider SDK lifecycle audit](PROVIDER_SDK_AUDIT.md)

## Plans and evidence

- [Product roadmap](../ROADMAP.md)
- [Roadmap evidence audit and external gates](ROADMAP_AUDIT.md)
- [Reproducible benchmarks](../bench/README.md)
- [Multi-agent guide](../MULTI_AGENT.md)
- [Sanitized showcase recipes](../showcase/)
- [Versioned release notes](releases/)

Fixture success proves LightClaw's local contracts. It does not prove live provider quality,
Telegram delivery, external coding-agent behavior, or production readiness.
