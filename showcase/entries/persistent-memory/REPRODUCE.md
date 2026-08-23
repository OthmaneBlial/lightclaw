# Reproduce

From an installed LightClaw checkout:

```bash
lightclaw demo --scenario memory --output ./output --json
```

Expected non-empty files:

- `output/artifact/recall.json`
- `output/receipt.json`
- `output/receipt.md`

Use a new or empty `output` directory. The command needs no token, Telegram account, model,
network call, or hidden manual step. Remove the output directory after inspection.

