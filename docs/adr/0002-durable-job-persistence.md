# ADR-0002: Durable Job Persistence

- Status: Accepted
- Date: 2026-08-23

## Context

Approved multi-agent work must survive handler interruptions, expose cancellation, avoid two writers claiming the same workspace, and preserve lane evidence without introducing a hosted queue.

## Decision

Use a private local SQLite `jobs.db` beside the memory database. A job records immutable approved scope plus lane dependencies, ownership, attempts, lease/heartbeat state, and terminal disposition. Workspace claims and state transitions are transactional. Startup recovers stale leases; retry remains bounded and only applies to declared idempotent/resumable work.

## Consequences

The design is single-host and intentionally not a distributed scheduler. SQLite state is evidence and control metadata, not a substitute for process-tree termination or artifacts. Schema migrations must preserve terminal history and fail closed on ambiguous ownership.
