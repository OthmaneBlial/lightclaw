"""
LightClaw — Infinite Memory
SQLite-backed persistent memory with TF-IDF vector similarity for RAG recall.
SQLite-based infinite memory with vector-based RAG.
"""

import json
import logging
import math
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("lightclaw.memory")

# ──────────────────────────────────────────────────────────────
# Text → Vector Embedding (lightweight TF-IDF with numpy)
# ──────────────────────────────────────────────────────────────

# Shared vocabulary built incrementally as new text is ingested.
# Maps word → index in the vector.
_vocab: dict[str, int] = {}
_idf: dict[str, float] = {}  # word → inverse document frequency
_doc_count: int = 0


def _tokenize(text: str) -> list[str]:
    """Simple whitespace + punctuation tokenizer, lowercased."""
    return re.findall(r"[a-zA-Z0-9\u00C0-\u024F]+", text.lower())


def _compute_embedding(text: str) -> bytes:
    """Compute a TF-IDF-ish vector for the given text and return as bytes."""
    global _doc_count
    tokens = _tokenize(text)
    if not tokens:
        return b""

    # Update vocabulary
    for t in tokens:
        if t not in _vocab:
            _vocab[t] = len(_vocab)

    # Term frequency (normalized)
    tf = Counter(tokens)
    max_freq = max(tf.values())

    vec = np.zeros(len(_vocab), dtype=np.float32)
    for word, count in tf.items():
        idx = _vocab[word]
        # Simple TF weighting
        vec[idx] = count / max_freq

    # Normalize to unit vector for cosine similarity
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm

    return vec.tobytes()


def _embedding_from_bytes(data: bytes) -> np.ndarray | None:
    """Reconstruct a numpy vector from stored bytes."""
    if not data:
        return None
    return np.frombuffer(data, dtype=np.float32)


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two vectors of potentially different lengths."""
    # Pad shorter vector to match longer
    max_len = max(len(a), len(b))
    if len(a) < max_len:
        a = np.pad(a, (0, max_len - len(a)))
    if len(b) < max_len:
        b = np.pad(b, (0, max_len - len(b)))

    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


# ──────────────────────────────────────────────────────────────
# Memory Store
# ──────────────────────────────────────────────────────────────


@dataclass
class MemoryRecord:
    id: int
    timestamp: float
    role: str
    content: str
    session_id: str
    similarity: float = 0.0


class MemoryStore:
    """
    Persistent memory with semantic recall.

    Uses SQLite for storage and TF-IDF vectors for similarity search.
    Each interaction is stored and can be recalled based on semantic
    similarity to a query — enabling "infinite memory" across sessions.
    """

    def __init__(self, db_path: str = "lightclaw.db"):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._rebuild_vocab()

    def _init_db(self):
        """Create tables if they don't exist."""
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                session_id TEXT NOT NULL,
                embedding BLOB
            );
            CREATE INDEX IF NOT EXISTS idx_interactions_session
                ON interactions(session_id);
            CREATE INDEX IF NOT EXISTS idx_interactions_timestamp
                ON interactions(timestamp);

            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                summary TEXT DEFAULT '',
                updated REAL
            );
        """)
        self.db.commit()

    def _rebuild_vocab(self):
        """Rebuild the vocabulary from all stored interactions on startup."""
        global _vocab, _doc_count
        cursor = self.db.execute("SELECT content FROM interactions")
        _doc_count = 0
        for (content,) in cursor:
            _doc_count += 1
            for token in _tokenize(content):
                if token not in _vocab:
                    _vocab[token] = len(_vocab)
        if _vocab:
            log.info(f"Rebuilt vocabulary: {len(_vocab)} terms from {_doc_count} interactions")

    # ── Ingest ────────────────────────────────────────────────

    def ingest(self, role: str, content: str, session_id: str):
        """Save an interaction and its embedding to the database."""
        global _doc_count
        if not content.strip():
            return

        embedding = _compute_embedding(content)
        _doc_count += 1

        self.db.execute(
            "INSERT INTO interactions (timestamp, role, content, session_id, embedding) VALUES (?, ?, ?, ?, ?)",
            (time.time(), role, content, session_id, embedding),
        )
        self.db.commit()

    # ── Recall (RAG) ──────────────────────────────────────────

    def recall(self, query: str, top_k: int = 5, exclude_session: str | None = None) -> list[MemoryRecord]:
        """
        Find the top_k most semantically similar past interactions.
        This is the RAG retrieval step — called before every LLM prompt.
        """
        query_embedding = _compute_embedding(query)
        if not query_embedding:
            return []
        query_vec = _embedding_from_bytes(query_embedding)
        if query_vec is None:
            return []

        # Fetch all embeddings (for small-to-medium datasets this is fine;
        # for very large DBs, switch to FAISS or similar)
        sql = "SELECT id, timestamp, role, content, session_id, embedding FROM interactions"
        params: list = []
        if exclude_session:
            sql += " WHERE session_id != ?"
            params.append(exclude_session)

        cursor = self.db.execute(sql, params)
        scored: list[tuple[float, MemoryRecord]] = []

        for row in cursor:
            rec_id, ts, role, content, sid, emb_bytes = row
            if not emb_bytes:
                continue
            stored_vec = _embedding_from_bytes(emb_bytes)
            if stored_vec is None:
                continue

            sim = _cosine_similarity(query_vec, stored_vec)
            if sim > 0.05:  # threshold
                record = MemoryRecord(
                    id=rec_id, timestamp=ts, role=role,
                    content=content, session_id=sid, similarity=sim,
                )
                scored.append((sim, record))

        # Sort by similarity descending, return top_k
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:top_k]]

    # ── Recent History ────────────────────────────────────────

    def get_recent(self, session_id: str, limit: int = 20) -> list[dict]:
        """Get recent messages for a session (for immediate context)."""
        cursor = self.db.execute(
            "SELECT role, content FROM interactions WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
            (session_id, limit),
        )
        rows = cursor.fetchall()
        rows.reverse()  # chronological order
        return [{"role": role, "content": content} for role, content in rows]

    # ── Session Summary ───────────────────────────────────────

    def get_summary(self, session_id: str) -> str:
        """Get the stored summary for a session."""
        cursor = self.db.execute(
            "SELECT summary FROM sessions WHERE session_id = ?", (session_id,)
        )
        row = cursor.fetchone()
        return row[0] if row else ""

    def set_summary(self, session_id: str, summary: str):
        """Store or update a session summary."""
        self.db.execute(
            "INSERT INTO sessions (session_id, summary, updated) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET summary = ?, updated = ?",
            (session_id, summary, time.time(), summary, time.time()),
        )
        self.db.commit()

    # ── Stats ─────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return memory statistics."""
        total = self.db.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
        sessions = self.db.execute("SELECT COUNT(DISTINCT session_id) FROM interactions").fetchone()[0]
        vocab_size = len(_vocab)
        return {
            "total_interactions": total,
            "unique_sessions": sessions,
            "vocabulary_size": vocab_size,
        }

    # ── Clear ─────────────────────────────────────────────────

    def clear_session(self, session_id: str):
        """Delete all interactions for a specific session."""
        self.db.execute("DELETE FROM interactions WHERE session_id = ?", (session_id,))
        self.db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        self.db.commit()

    def format_memories_for_prompt(self, memories: list[MemoryRecord]) -> str:
        """Format recalled memories for injection into the system prompt."""
        if not memories:
            return ""

        lines = ["## Recalled Memories", ""]
        for m in memories:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.timestamp))
            lines.append(f"- [{ts}] {m.role}: {m.content[:200]}")
        return "\n".join(lines)
