# Reproducible Benchmarks

LightClaw does not use “tiny,” “infinite,” or “runs anywhere” as unbounded claims. This suite records what was actually measured and labels fixtures separately from live installation evidence.

## Fast fixture evidence

```bash
python -m bench.run --mode fixture --runs 5 --output bench/results/fixture
```

This records startup time, an idle import-process RSS sample, configuration-routing overhead, runtime Python LOC, direct runtime dependency count, retrieval top-1 quality on the versioned local corpus, and the deterministic DAG/failure/repair contract.

## Full clean-install evidence

```bash
python -m build
python -m bench.run \
  --mode full \
  --runs 5 \
  --wheel dist/lightclaw_ai-0.1.0.dev0-py3-none-any.whl \
  --output bench/results/full
```

Full mode creates a new virtual environment, resolves the wheel and runtime dependencies through pip, records install time and installed distribution count, and smoke-tests the installed command. Network and pip-cache state are explicitly disclosed in the JSON.

Every result includes commit, timestamp, OS, Python, architecture, processor, CPU count, memory, run count, and evidence mode. Raw JSON is canonical; CSV is a flattened convenience export.

The current bounded container evidence is a successful image build, command smoke, real fixture unit test, and receipt generation at `256 MiB` and `0.5 CPU`. It is not evidence that a live Telegram bot plus provider and external coding agent fits that limit. Reproduce it with `bash bench/container_smoke.sh`.
