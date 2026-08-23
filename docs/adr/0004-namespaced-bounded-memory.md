# ADR-0004: Namespaced Bounded Memory

- Status: Accepted
- Date: 2026-08-23

## Context

Local chat recall must not leak across users/workspaces, grow without limit, or claim semantic understanding without evidence.

## Decision

Use SQLite FTS5 lexical retrieval with explicit user and workspace namespaces, per-request context binding, bounded record/database retention, bounded candidates/terms, and a query deadline. Export, selective delete, scoped clear, and prune are previewable local data operations. Optional embedding adapters may rerank lexical candidates only and must record provenance; no embedding provider ships enabled.

## Consequences

Lexical misses remain possible and are reported by the versioned evaluation corpus. Memory is local storage, while prompt context can still be sent to the configured hosted model. Hybrid quality claims require a real adapter-specific evaluation rather than the deterministic fixture.
