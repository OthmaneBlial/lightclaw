# Maintenance, Releases, and Community Updates

LightClaw uses evidence channels that remain useful after launch:

- [GitHub Releases](https://github.com/OthmaneBlial/lightclaw/releases) for immutable version
  notes, distributions, attestations, runtime footprint, limitations, and rollback links;
- [Development updates and release evidence](https://github.com/OthmaneBlial/lightclaw/discussions/20)
  for the recurring update thread;
- [Discussions Q&A](https://github.com/OthmaneBlial/lightclaw/discussions/categories/q-a)
  for support and [Ideas](https://github.com/OthmaneBlial/lightclaw/discussions/categories/ideas)
  for design before implementation;
- [CHANGELOG.md](../CHANGELOG.md) for unreleased repository changes;
- [launch/status.json](../launch/status.json) for stages that must not be inferred.

The intended cadence is monthly or explicitly paused. A pause is posted with its reason,
current security/support status, and next review date. An update should contain shipped
commits/releases, actual fixes, raw benchmark changes, consented workflows, current
limitations, and response metrics with a denominator. It should not repeat an announcement
or infer adoption from stars, traffic, forks, or private messages.

## Repository discovery contract

Maintainer-controlled target, reverified on 2026-09-01 before publication:

- homepage URL: `https://othmaneblial.github.io/lightclaw/`;
- topics: `ai-agent`, `claude-code`, `codex`, `coding-agent`, `developer-tools`,
  `human-in-the-loop`, `local-first`, `multi-agent`, `python`, `remote-coding`,
  `remote-control`, `self-hosted`, and `telegram-bot`;
- community profile: 100%, backed by the actual README, MIT license, contribution guide,
  Code of Conduct, PR template, structured issue forms, security policy, and support routes;
- Discussions and private vulnerability reporting: enabled.

These are maintainer-controlled settings and should be rechecked before a release rather
than treated as permanent facts.

## Badge admission policy

A README badge must link directly to a signal that exists and can be independently opened.
The current README intentionally uses no badges so the product story and evidence lead the
page. Do not add stars, downloads, coverage, release, container, PyPI, security-grade, or
compatibility badges until the linked public signal is live and its scope is accurately
labeled. Remove a badge when its source is retired.

Marketing milestones never override failed CI, an open critical/high security finding, an
unverified distribution, missing rollback instructions, or an unsatisfied external alpha
gate.
