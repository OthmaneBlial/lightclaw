"""Run the versioned lexical/fixture-hybrid memory retrieval evaluation."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

from memory import MemoryStore, _tokenize

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = PROJECT_ROOT / "bench" / "fixtures" / "memory_eval_v1.json"


class CorpusEmbeddingAdapter:
    name = "fixture-synonym-groups"
    version = "1"

    def __init__(self, groups: list[list[str]]):
        self.groups = [set(str(term).lower() for term in group) for group in groups]

    def embed(self, text: str):
        tokens = set(_tokenize(text))
        values = [float(len(tokens & group)) for group in self.groups]
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values] if norm else [0.0 for _ in values]


def _git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def _evaluate(corpus: dict[str, object], *, hybrid: bool, top_k: int) -> dict[str, object]:
    groups = corpus.get("embedding_groups")
    adapter = CorpusEmbeddingAdapter(groups) if hybrid and isinstance(groups, list) else None
    temporary = Path(tempfile.mkdtemp(prefix="lightclaw-memory-eval-"))
    store = MemoryStore(str(temporary / "memory.db"), embedding_adapter=adapter)
    interaction_to_document: dict[int, str] = {}
    documents = corpus.get("documents") if isinstance(corpus.get("documents"), list) else []
    for document in documents:
        if not isinstance(document, dict):
            continue
        scope = str(document.get("scope") or "main")
        user = "eval-user" if scope != "other-user" else "other-user"
        workspace = "eval-workspace" if scope not in {"other", "other-user"} else "other-workspace"
        identifier = store.ingest(
            "fixture",
            str(document.get("content") or ""),
            f"document:{document.get('id')}",
            user_namespace=user,
            workspace_namespace=workspace,
        )
        if identifier is not None:
            interaction_to_document[identifier] = str(document.get("id"))

    latencies: list[float] = []
    precision_values: list[float] = []
    recall_values: list[float] = []
    reciprocal_ranks: list[float] = []
    details: list[dict[str, object]] = []
    leaked_results = 0
    queries = corpus.get("queries") if isinstance(corpus.get("queries"), list) else []
    for query in queries:
        if not isinstance(query, dict):
            continue
        started = time.perf_counter()
        records = store.recall(
            str(query.get("text") or ""),
            top_k=top_k,
            user_namespace="eval-user",
            workspace_namespace="eval-workspace",
        )
        latencies.append((time.perf_counter() - started) * 1_000)
        returned = [interaction_to_document[record.id] for record in records]
        relevant = {str(value) for value in query.get("relevant", [])}
        hits = [document for document in returned if document in relevant]
        precision = len(hits) / top_k
        recall = len(hits) / max(1, len(relevant))
        reciprocal = next(
            (1 / rank for rank, document in enumerate(returned, start=1) if document in relevant),
            0.0,
        )
        leaks = [document for document in returned if document.startswith("private-other")]
        leaked_results += len(leaks)
        precision_values.append(precision)
        recall_values.append(recall)
        reciprocal_ranks.append(reciprocal)
        details.append(
            {
                "query_id": query.get("id"),
                "returned": returned,
                "relevant": sorted(relevant),
                "precision_at_k": round(precision, 4),
                "recall_at_k": round(recall, 4),
                "reciprocal_rank": round(reciprocal, 4),
            }
        )
    stats = store.stats(user_namespace="eval-user", workspace_namespace="eval-workspace")
    store.db.close()
    return {
        "mode": "fixture hybrid rerank" if hybrid else "sqlite fts5 lexical",
        "top_k": top_k,
        "queries": len(details),
        "mean_precision_at_k": round(statistics.mean(precision_values), 4),
        "mean_recall_at_k": round(statistics.mean(recall_values), 4),
        "mean_reciprocal_rank": round(statistics.mean(reciprocal_ranks), 4),
        "query_latency_ms": {
            "median": round(statistics.median(latencies), 4),
            "p95": round(_percentile(latencies, 0.95), 4),
            "max": round(max(latencies), 4),
        },
        "database_bytes": stats["database_bytes"],
        "cross_namespace_results": leaked_results,
        "details": details,
    }


def build_report(corpus_path: Path, top_k: int) -> dict[str, object]:
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    lexical = _evaluate(corpus, hybrid=False, top_k=top_k)
    hybrid = _evaluate(corpus, hybrid=True, top_k=top_k)
    return {
        "schema_version": 1,
        "corpus": corpus.get("name"),
        "corpus_schema_version": corpus.get("schema_version"),
        "commit": _git_commit(),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "sqlite": __import__("sqlite3").sqlite_version,
            "pid": os.getpid(),
        },
        "disclosure": (
            "The hybrid adapter is a deterministic synonym-group fixture, not a semantic model. "
            "Results characterize this versioned corpus only."
        ),
        "lexical": lexical,
        "fixture_hybrid": hybrid,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "bench" / "results" / "memory-eval-v1.json",
    )
    args = parser.parse_args()
    report = build_report(args.corpus.resolve(), max(1, min(10, args.top_k)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": args.output.as_posix(), "commit": report["commit"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
