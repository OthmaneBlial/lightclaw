# Local Lexical Memory

LightClaw memory is local, bounded SQLite FTS5 lexical search. It matches words; it does not claim semantic understanding. An optional embedding adapter may rerank the bounded lexical candidates, but FTS5 remains the baseline and no embedding service is required.

## Isolation contract

Every interaction and summary carries both:

- a user namespace, such as `telegram-user:<id>` or `cli-user:<id>`;
- a resolved workspace namespace.

Recall, recent history, summaries, export, and deletion require one exact pair. The same Telegram chat cannot leak one user’s records to another user, and the same user cannot recall records from another workspace. Pre-migration rows are retained in isolated `legacy-session:<session>` / `legacy-default` scopes instead of being guessed into a new identity.

## Data map

The default database is `~/.lightclaw/lightclaw.db` with mode `0600`. SQLite may create `lightclaw.db-wal` and `lightclaw.db-shm` beside it while the process is active. Anyone who can access the host account may read this data; LightClaw does not claim encryption at rest.

| Table | Local contents |
|---|---|
| `interactions` | timestamp, role, bounded message text, session, user/workspace namespaces |
| `interactions_fts` | SQLite FTS5 lexical index of interaction text |
| `memory_scopes` | current session-to-user/workspace binding |
| `memory_summaries` | summaries keyed by session plus both namespaces |
| `memory_embeddings` | optional adapter name/version and numeric vectors; empty by default |
| `memory_meta` | schema version |
| `sessions` | legacy summary table retained for migration compatibility |

The separate `jobs.db` stores durable run state, not chat memory. Receipts, artifacts, and task workspaces are also separate; see [Run Receipts](RUN_RECEIPTS.md), [Job Control](JOB_CONTROL.md), and [Git Artifacts](ARTIFACTS.md).

## Bounds and retention

Defaults are explicit and configurable:

```dotenv
MEMORY_RETENTION_DAYS=90
MEMORY_MAX_INTERACTIONS=10000
MEMORY_MAX_DB_MB=64
MEMORY_QUERY_TIMEOUT_MS=100
MEMORY_CANDIDATE_LIMIT=200
```

One stored interaction is capped at 32,000 characters. Ingest prunes expired and oldest overflow records. FTS queries accept at most 16 unique query terms, return at most 50 results, inspect at most the configured candidate count, and use a SQLite progress deadline. `lightclaw memory status` reports current size, limits, retrieval mode, last query time, and timeout count.

These are safety ceilings, not performance promises for every disk or host. The versioned evaluation under `bench/fixtures/` publishes measured precision, recall, reciprocal rank, query latency, and database size for its named environment.

## Inspect and export

Terminal sessions use their bound `cli` scope by default:

```bash
lightclaw memory status
lightclaw memory export --output ./private-memory-export.json
```

Export is preview-only until `--apply` is added. The resulting JSON is owner-only and contains the exact record fields listed in the preview.

An administrator can target an exact Telegram/user workspace pair without broadening recall:

```bash
lightclaw memory status \
  --user telegram-user:123456 \
  --workspace /absolute/path/to/workspace
```

## Selective delete, scope clear, and prune

Every destructive CLI action is preview-first:

```bash
lightclaw memory delete --ids 41 42
lightclaw memory clear
lightclaw memory prune
```

After checking the matched IDs or count, add `--apply`. A record ID outside the selected user/workspace scope is never deleted. Telegram `/clear` removes only the current bound session; `/wipe_memory` still requires its separate time-bounded confirmation and removes all local memory.

## Optional embeddings

`memory.EmbeddingAdapter` is a small typed protocol with `name`, `version`, and `embed(text)`. Vectors are stored with adapter provenance and can only rerank FTS5 candidates. Adapter errors fall back to the lexical record or lexical query result. LightClaw ships no default embedding provider, sends no memory to an embedding service by default, and does not call lexical retrieval “semantic.”
