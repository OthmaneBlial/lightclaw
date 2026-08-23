# Privacy-safe Alpha Evidence

The release gate needs reports from 10–20 real external self-hosters. Raw reports are
private maintainer evidence and never belong in Git. The public
[`aggregate.json`](aggregate.json) contains only consented counts, bounded timings, tested
environment categories, missing values, and deterministic gate states.

## Collect

1. Invite a tester to follow the [five-minute quickstart](../../docs/QUICKSTART.md) and
   submit the public, sanitized `Alpha installation report` issue form or the
   [private report template](../ALPHA_REPORT_TEMPLATE.md).
2. Confirm the person is external, consented to an anonymous aggregate, and supplied no
   prompt, receipt, path, repository, Telegram identity, token, or free-text content in the
   structured record.
3. Treat every issue-form dropdown as conservative by default. Transcribe a success only
   when the tester explicitly selected it, and normalize `Other/unspecified` to `other`.
   Reject Python versions outside the supported 3.10–3.13 range instead of guessing.
4. Create a random opaque ID matching `alpha-[0-9a-f]{12}` and transcribe only the fields
   allowed by [`report.schema.json`](report.schema.json) into a local directory such as
   `evidence/private-alpha/`. That directory is ignored by Git.
5. Generate and inspect the aggregate:

   ```bash
   python scripts/aggregate_alpha_reports.py \
     --input evidence/private-alpha \
     --output launch/alpha/aggregate.json

   python scripts/aggregate_alpha_reports.py \
     --input evidence/private-alpha \
     --check launch/alpha/aggregate.json
   ```

   Inspect the aggregate diff before running the comparison and canonical quality command.
   Never commit the private input directory.

## Gate semantics

- 10–20 unique consented external reports, with at least 10 fresh install attempts;
- at least 9 deterministic demo successes;
- at least 9 measured deterministic successes with median below 180 seconds;
- at least one measured real Telegram success with median below 600 seconds.

Failures and missing timing values remain visible in the denominator. The script rejects
identity/free-text fields and cannot mark a gate from prose or from maintainer fixture runs.
