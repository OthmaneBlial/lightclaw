# Security Policy

LightClaw can receive remote Telegram input and delegate work to local coding-agent CLIs. Treat it as privileged developer tooling, not as a hardened multi-tenant service.

## Supported versions

| Version | Security fixes |
|---|---|
| Latest release on the `0.1.x` line | Yes |
| Current `main` branch | Best effort; may be unstable |
| Older snapshots and forks | No |

Until `v0.1.0` is released, only the current `main` branch receives security fixes.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or paste credentials, bot tokens, private prompts, receipts, or workspace contents into an issue.

Use [GitHub private vulnerability reporting](https://github.com/OthmaneBlial/lightclaw/security/advisories/new). If that form is unavailable, open a minimal public issue asking for a private contact channel without including technical details.

We aim to acknowledge a complete report within 3 business days, provide an initial assessment within 7 business days, and coordinate disclosure after a fix is available. These are response targets, not a bounty or legal guarantee.

Useful reports include the affected commit/version, the configured capability profile, reproducible steps using disposable data, impact, and any proposed mitigation.

## Security defaults

- Telegram access fails closed unless numeric owner IDs are configured. Public access requires `LIGHTCLAW_PUBLIC_BOT_ACK=yes`.
- Delegated agents receive a minimal environment that excludes provider keys, Telegram tokens, and unrelated host secrets.
- The default delegation profile is `workspace-write`; `trusted-command` requires confirmation for each run.
- Coding tasks run inside per-task directories recorded as LightClaw-owned.
- Skills install inactive. Activation requires review of source, provenance, permissions, and a hash that binds `SKILL.md` to `skill.json`; only prompt-guidance skills enter the core prompt.
- `lightclaw undo` and `lightclaw uninstall` are dry runs unless explicitly applied.
- Logs and provider errors pass through credential redaction, but users must still avoid placing secrets in prompts or source files.

## Host boundary

LightClaw cannot protect the host after a user explicitly grants trusted host execution, activates malicious prompt guidance, weakens external CLI sandbox settings, exposes the Telegram bot publicly, or runs the process with access to sensitive host files. Skill manifests constrain LightClaw's extension loader; they do not make untrusted instructions trustworthy. Regex command blocks are defense in depth and are not a sandbox.

See [the threat model](docs/THREAT_MODEL.md) for boundaries and assumptions.
