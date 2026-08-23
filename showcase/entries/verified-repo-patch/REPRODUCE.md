# Reproduce

From an installed LightClaw checkout:

```bash
lightclaw demo --scenario repo-task --output ./output --json
```

Expected non-empty files:

- `output/artifact/test-output.txt`
- `output/phone-to-patch.json`
- `output/review/changes.patch`
- `output/review/artifact.json`
- `output/receipt.json`

Use a new or empty `output` directory. The command has zero hidden manual steps and needs
no token, Telegram account, model, network call, or source repository.

