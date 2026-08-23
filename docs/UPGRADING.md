# Upgrade and Migration Policy

LightClaw uses semantic versioning after `v0.1.0`. During the `0.x` series, a minor release may change alpha interfaces; every migration must be called out in the changelog and release notes.

## Before upgrading

1. Stop the running bot and any delegated workers.
2. Run `lightclaw doctor --json` and keep the redacted result.
3. Back up `~/.config/lightclaw/` and `~/.lightclaw/` with permissions preserved.
4. Read every changelog entry between the installed and target versions.
5. Upgrade through the same installer (`pipx`, `uv`, or compatibility script) used originally.
6. Run the deterministic demo and doctor before restarting Telegram polling.

## Configuration migration

The supported config path is `~/.config/lightclaw/config.env`. LightClaw may copy a legacy `~/.env` into that path once, but never deletes the legacy source. A reset creates a timestamped mode-`0600` backup.

New settings receive secure defaults. A release that requires a manual setting change must include the exact old/new key and a rollback step in its notes.

## Data migration

SQLite schema changes must be forward-only, transactional, and covered by a fixture that opens data from the previous released schema. Until such a migration exists, do not rewrite a user's database during onboarding.

Receipts keep an explicit `schema_version`. Readers should reject unsupported future versions rather than guessing.

## Rollback

Reinstall the previous release tag, restore the matching config/data backup if a documented migration occurred, and run `lightclaw doctor`. `lightclaw undo` rolls back one LightClaw-owned task directory; it is not an application-version rollback.
