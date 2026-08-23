# LightClaw Showcase

This directory turns a successful workflow into a small, inspectable reason for the next
person to try LightClaw:

```text
use a workflow -> export a sanitized Run Card -> submit it -> get featured
        ^                                                    |
        +-------------- fork the recipe/skill <--------------+
```

The entries are public evidence, not runtime storage. LightClaw never scans or uploads
your receipts, prompts, repository names, local paths, or analytics. A person must first
export a local Run Card with `lightclaw run export --apply`, review every field, place a
deliberately public copy into a new entry, and open a pull request.

## Curated starting recipes

| Entry | What it proves | Token/network cost |
|---|---|---|
| [Persistent memory](entries/persistent-memory/) | A synthetic fact survives a SQLite restart and is recalled in its namespace | none |
| [Verified repository patch](entries/verified-repo-patch/) | A phone-shaped request produces a Git patch and a passing real unit test | none |
| [Audited multi-agent handoff](entries/audited-multi-agent/) | A dependency, failed acceptance check, bounded repair, and final audit are recorded | none |

These three entries are maintainer-authored deterministic fixtures. They are not described
as community submissions and do not satisfy the community milestone in the roadmap.

## Entry contract

Each entry is independently forkable as a recipe or permission-manifest skill and contains:

- `prompt.md` with synthetic or explicitly publishable input;
- `setup.md` with credentials, local paths, and repository identity removed;
- `result.md` with observable output and limitations;
- `run-card.json`, never a raw private receipt;
- `REPRODUCE.md` with a bounded command and expected files;
- `recipe.json`, or a validated `SKILL.md` plus `skill.json` for a skill entry;
- `showcase.json` with provenance, consent, privacy attestations, and file mapping.

Run the same gate as CI:

```bash
python scripts/validate_showcase.py --execute
```

The validator rejects known credential shapes, user-home paths, symlinks, oversized/binary
evidence, private receipt fields, missing provenance, and recipes requiring credentials or
network access. It then replays every recipe and checks its declared artifacts. This is a
bounded safety gate, not proof that arbitrary text is anonymous; reviewers must still read
the complete diff.

## Submit or feature a workflow

1. Copy the smallest relevant entry directory and change its slug.
2. Use synthetic names and data; never copy a raw receipt into Git.
3. Declare community provenance, a durable public source URL, and explicit publication
   consent in `showcase.json`.
4. Run the validator and canonical quality command.
5. Open the [showcase form](https://github.com/OthmaneBlial/lightclaw/issues/new?template=showcase.yml)
   or a pull request using the evidence template.

`featured.json` remains `null` until a real external submission passes privacy review and
the author consents. A release can enforce that policy with:

```bash
python scripts/validate_showcase.py --execute --require-community-feature
```

