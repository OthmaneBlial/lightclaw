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

## Versioned memory evaluation

Run the FTS5 lexical baseline and the explicitly fixture-only hybrid reranker against the public corpus:

```bash
python -m bench.memory_eval --output bench/results/memory-eval-v1.json
```

The report publishes precision@k, recall@k, mean reciprocal rank, query latency, database size, per-query results, and cross-namespace leakage count. The synonym-group hybrid adapter is deterministic test machinery, not evidence of semantic understanding on arbitrary text. See [the memory contract](../docs/MEMORY.md).

## Published evidence

The first full run is tied to commit `be5a71d30cd7a2bea5119d2624fdeff275c5c8da`:

| Measurement | Result | Scope |
|---|---:|---|
| Clean wheel install | 12.790 s | Live PyPI resolution; local pip cache may be used |
| Installed distributions | 45 | Includes direct and transitive runtime dependencies |
| Startup median | 100.190 ms | Five fresh `import lightclaw_cli` subprocesses |
| Idle import-process RSS | 24.94 MiB | One imported process sampled with `ps` |
| Config routing overhead | 1.6367 microseconds/call | 100,000 deterministic provider/model resolutions |
| Runtime Python LOC | 13,291 | Runtime modules only; tests and benchmarks excluded |
| Direct runtime dependencies | 6 | Distribution metadata at the measured commit; current base-package counts are emitted by each new run |
| Memory retrieval fixture | 8/8 top-1 | Versioned deterministic lexical corpus |
| Orchestration fixture | 4/4 contracts | Dependency, handoff, failure, and bounded repair |

Conditions: macOS 26.6, Apple ARM64, Python 3.13.1, 8 logical CPUs, 16 GiB RAM, five timing runs. Read the canonical [raw JSON](results/2026-08-23-macos-arm64-py313.json) or [flattened CSV](results/2026-08-23-macos-arm64-py313.csv). These measurements describe this machine and fixture mode; they are not guarantees for live Telegram or hosted-provider workloads.

The current bounded container evidence is a successful image build, command smoke, real fixture unit test, and receipt generation at `256 MiB` and `0.5 CPU`. It is not evidence that a live Telegram bot plus provider and external coding agent fits that limit. Reproduce it with `bash bench/container_smoke.sh`.
