# Evidence-Led Launch Pack

This pack is ready to use; the launch stages are not claimed as completed. The canonical
[status file](status.json) currently records zero external alpha reports, no stable release,
and no public launch. Update a stage only with a durable evidence URL or a versioned,
privacy-safe aggregate.

## Reusable pack

| Required asset | Canonical source | Evidence boundary |
|---|---|---|
| One headline | [HEADLINE.md](HEADLINE.md) | One factual product outcome, no star promise |
| One short clip | [24-second silent demo](../assets/demo.svg) | Deterministic fixture, captioned; no live-provider claim |
| One five-minute quickstart | [Token-free quickstart](../docs/QUICKSTART.md) | Uses a disposable generated repository |
| Three recipes | [Showcase](../showcase/) | Maintainer fixtures; not community submissions |
| Architecture/security diagram | [Trust-boundary diagram](../assets/security-architecture.svg) | Shows where context leaves the host |
| Raw benchmark data | [Benchmark results](../bench/results/) | Machine/commit/mode disclosed in JSON and CSV |
| Honest comparison tables | [COMPARISONS.md](COMPARISONS.md) | Categories and verified LightClaw facts, not rankings |

The machine-readable [manifest](manifest.json) and `python scripts/check_launch_pack.py`
prevent missing assets or silently inflated readiness claims.

## Stage gates

1. **Private alpha (10–20 self-hosters).** Invite people individually. Collect the
   [bounded report](ALPHA_REPORT_TEMPLATE.md) without credentials, prompts, repository
   names, or raw receipts. Publish only an aggregate with denominator and failures.
2. **`v0.1.0`.** Require secure packaging, supported CI, deterministic demo, three recipes,
   release notes, rollback instructions, and no open critical/high security finding.
3. **`v0.2.0`.** Require Run Receipts, approvals, durable jobs, review artifacts, and a
   real captioned Telegram verification. Fixture SVGs are not a substitute for that video.
4. **Public launch.** Publish the verified GitHub Release first, then adapt the same
   [channel templates](CHANNEL_TEMPLATES.md) for a technical article, Show HN, relevant
   Python/self-hosted communities, and Telegram demo posts. Respect each community's rules.
5. **Follow-up.** Use [FOLLOW_UP.md](FOLLOW_UP.md) to publish fixes, limitations, real
   workflows, benchmark changes, and paused milestones instead of repeating the headline.

No script in this repository posts to a community, uploads a receipt, recruits users, or
changes launch status automatically. Those are deliberate maintainer actions that require
current evidence and channel-specific judgment.

