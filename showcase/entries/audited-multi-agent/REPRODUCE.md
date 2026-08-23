# Reproduce

From an installed LightClaw checkout:

```bash
lightclaw demo --scenario multi-agent --output ./output --json
```

Expected non-empty files:

- `output/artifact/handoff/research.json`
- `output/artifact/handoff/initial-failure.json`
- `output/artifact/handoff/repair.json`
- `output/artifact/final-audit.json`
- `output/receipt.json`

Use a new or empty `output` directory. The command is fully deterministic and requires no
provider, local coding-agent CLI, Telegram account, credential, or network access.

