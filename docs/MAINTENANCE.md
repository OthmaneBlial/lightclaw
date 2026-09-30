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

The intended cadence is monthly or explicitly paused. At each review, recheck the README,
GitHub Pages copy and metadata, repository description and topics, and workflow status against
current product and release evidence. A pause is posted with its reason, current security/support
status, and next review date. An update should contain shipped
commits/releases, actual fixes, raw benchmark changes, consented workflows, current
limitations, and response metrics with a denominator. It should not repeat an announcement
or infer adoption from stars, traffic, forks, or private messages.

## Repository discovery contract

Maintainer-controlled target, reverified on 2026-09-30:

- homepage URL: `https://othmaneblial.github.io/lightclaw/`;
- repository description: `Review-first Telegram missions for local Codex and Claude agents. Approve scope, then inspect checks, patches, and private receipts.`;
- topics: `agent-orchestration`, `audit-trail`, `claude-code`, `code-review`, `codex`,
  `coding-agent`, `developer-tools`, `human-in-the-loop`, `local-first`, `multi-agent`,
  `python`, `remote-coding`, `self-hosted`, `telegram`, and `telegram-bot`;
- community profile: 100%, backed by the actual README, MIT license, contribution guide,
  Code of Conduct, PR template, structured issue forms, security policy, and support routes;
- Discussions and private vulnerability reporting: enabled.

These are maintainer-controlled settings and should be rechecked before a release rather
than treated as permanent facts.

## Badge admission policy

Each README badge must link to its current source: Python and OS support link to the
installation contract, alpha status links to `launch/status.json`, and the license links to
`LICENSE`. Do not add stars, downloads, coverage, release, container, PyPI, security-grade,
or compatibility metrics until each linked public signal is live and its scope is accurately
labeled. Remove a badge when its source is retired.

Marketing milestones never override failed local quality checks, an open critical/high
security finding, an unverified distribution, missing rollback instructions, or an
unsatisfied external alpha gate.
