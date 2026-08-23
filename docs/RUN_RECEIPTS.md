# Run Receipts and Run Cards

Every delegated single-agent and multi-agent run writes a JSON receipt and a Markdown receipt outside the task tree, under the private `.lightclaw-meta/receipts/` directory in the configured workspace root. Files and receipt directories are owner-readable only.

The receipt records the approved goal and scope, risk/capability, DAG and worker assignment, bounded usage when available, redacted commands and exit evidence, content-addressed file changes, checks, handoffs, failures, retries, checkpoint, disposition, and scoped undo command. A `null` token or cost value means the local coding-agent CLI did not expose that evidence; LightClaw does not invent an estimate.

## Validate the private receipt

The JSON `schema_version` is stable and the CLI refuses incomplete receipts. Keep the private receipt local because it may contain workspace paths, commands, handoffs, and recovery context.

## Preview a sanitized Run Card

```bash
lightclaw run export --receipt /path/to/receipt.json
```

Preview mode prints the exact included and excluded field names and writes nothing. It omits commands, handoffs, checkpoints, undo details, and timestamps. Known credentials and common token formats are redacted again.

## Write the reviewed card

```bash
lightclaw run export \
  --receipt /path/to/receipt.json \
  --output ./run-card.json \
  --apply
```

The resulting card still uses private file permissions. Read it before voluntarily sharing
it; LightClaw never uploads a receipt or card by default. Public examples belong in the
[showcase](../showcase/) only after its provenance, privacy, and reproducibility gate passes.
See the complete [privacy and public-evidence boundary](PRIVACY.md).
