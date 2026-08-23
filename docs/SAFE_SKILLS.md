# Build a Safe Skill in 10 Minutes

A LightClaw skill is reviewed prompt guidance plus a mandatory permission manifest. It is not an executable plugin. The core activates only valid prompt-only skills; anything requesting network, writable paths, subprocesses, dependencies, or trusted commands remains `isolated-only` until a separate external runner exists.

## Minute 1: create two files

```text
my-safe-skill/
├── SKILL.md
└── skill.json
```

Start `SKILL.md` with a narrow purpose and testable rules:

```markdown
---
name: Issue Triage Helper
description: Turn a bug report into a reproducible local checklist.
---

# Issue Triage Helper

1. Restate the observed and expected behavior.
2. Ask for the smallest missing reproduction detail.
3. Never claim a bug is fixed without test evidence.
```

## Minutes 2–4: declare exact permissions

Create `skill.json`:

```json
{
  "schema_version": 1,
  "id": "issue-triage-helper",
  "name": "Issue Triage Helper",
  "version": "1.0.0",
  "owner": "your-github-handle",
  "capabilities": ["prompt-guidance"],
  "network": {"allowed": false, "domains": []},
  "writable_paths": [],
  "dependencies": []
}
```

Versions and dependencies must be pinned. Paths must be relative and cannot traverse outside a workspace. Network access requires both the `network` capability and explicit domains, but such a skill is intentionally blocked from the core prompt and labeled `isolated-only`.

## Minutes 5–6: validate without execution

```bash
lightclaw skills validate --path ./my-safe-skill
```

Validation reads bounded `SKILL.md` and `skill.json` files. It does not import code, install dependencies, contact a network, or activate the skill. The command exits non-zero for malformed permissions, unpinned versions/dependencies, symlinks, oversized files, traversal paths, or ambiguous archives.

Use the repository example as a known-good contract:

```bash
lightclaw skills validate --path examples/safe-skill
```

## Minutes 7–8: install locally

Telegram `/skills create <name> [description]` creates an inactive prompt-only skill under the private runtime. Edit its two files locally, then validate its directory. Hub installs also remain inactive and pin the concrete owner, version, download URL, archive hash, manifest hash, content hash, and install time in `source.json`.

No skill may add a Python package to LightClaw core. Declare executable dependencies as exact `name==version` entries with the `subprocess` capability; the core will keep that skill isolated rather than installing or executing them.

## Minutes 9–10: preview and activate

In Telegram:

```text
/skills use local/issue-triage-helper
```

LightClaw shows the source excerpt, owner, pinned version, capabilities, network domains, writable paths, dependencies, full content SHA-256, and a short activation token. After reviewing every field, repeat the generated command containing that exact hash token.

Changing `SKILL.md` or `skill.json` changes the token, so stale approval cannot silently activate new instructions or permissions. Invalid or high-authority skills never enter prompt context.

## CI contract

Community skill repositories should run:

```bash
lightclaw skills validate --path path/to/skill
```

LightClaw CI validates [the sample skill](../examples/safe-skill/) on every change. Copy that check into a skill repository before sharing it. Validation proves the manifest contract and file boundaries; it does not certify the quality or intent of the instructions.
