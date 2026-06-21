# ADR 002: Append-Only Journal as Source of Truth for Task Progress

## Status
Accepted (Phase 1 — schema and read path); the write-side replay/resume
logic this enables is implemented in Phase 3 alongside the agent loop.

## Context
Two requirements push in the same direction:
- **Resumability** (§2.2, §3): a crashed or interrupted run must resume
  "without re-issuing paid model calls or re-applying side effects."
- **Streamability** (§2.2): the caller watches progress live over SSE, and
  per the operability requirement (§3) we need per-run traces anyway.

If "current task state" lived only in a mutable `tasks.state` column, a
crash between "model call succeeded" and "state column updated" would lose
exactly the information needed to avoid re-paying for that model call on
resume. We need a record of *what already happened*, not just *what state
we're currently in*.

## Decision
`journal_events` is an append-only table: `(task_id, step_index,
event_type, payload, created_at)`, with a unique constraint on
`(task_id, step_index)`. The orchestrator (Phase 3) writes a journal row
**before** executing the side effect it describes is committed as fact —
e.g. it commits "tool_call: read_span(file, 10, 40)" with the result
already attached, atomically, so there's never a window where a side
effect happened but isn't recorded.

On resume, the orchestrator's first action is to read the full journal for
the task in `step_index` order and replay it into in-memory state (last
plan, accumulated context, budget spent so far) without re-invoking the LLM
or re-running tools for any `step_index` already present. It only issues
new model/tool calls for the step *after* the last journaled one. This is
what makes "no double-charging, no re-applying side effects" true rather
than aspirational.

`budget_ledger` follows the same append-only pattern for the same reason:
the running spend is `SUM(amount) WHERE task_id = ? AND dimension = ?`,
computed from immutable rows, not a counter that can be incremented twice
by a re-executed step.

SSE streaming reads from this same table (`GET /v1/tasks/{id}/events`
polls `journal_events WHERE step_index > last_seen`) rather than an
in-process pub/sub channel. Consequence: the journal table is not just a
debugging aid, it *is* the live progress feed — there is one source of
truth for "what has this task done so far," read by both the resume path
and the streaming path. A second benefit we get for free: a client can
disconnect and reconnect to the SSE endpoint, or two clients can watch the
same task, and both see full, identical history, because neither is
reading from a transient buffer.

## Delivery guarantee, stated explicitly
**At-least-once execution of side effects, exactly-once accounting.**
A side effect (a tool call, a model call) and its journal row are written
in the same DB transaction, so they're atomic with respect to a crash —
but if the *process* crashes between performing the external side effect
(e.g. the Anthropic API call returns) and committing the transaction, the
side effect happened in the world but isn't journaled, and Phase 3's
replay will re-issue it. This is an explicit gap: it trades a small
probability of a double model-call (cost, not correctness, since model
calls are idempotent reads-of-the-world) against the alternative of
journaling speculatively before the side effect completes, which would
risk claiming a tool call happened when it didn't. We chose to fail toward
"might re-pay a few cents" rather than "might silently skip a step."
Sandbox verification runs (Phase 4) get an additional safeguard: the
verification result includes a content hash of the patch it verified, so a
replayed "verify" step that finds an already-recorded result for the
identical patch skips re-running the sandbox rather than re-billing
sandbox-seconds.

## Alternatives considered
- **Event-sourced state machine with snapshots.** More principled for very
  long-running tasks, but task runs here are bounded by `max_wall_seconds`
  (≤ tens of minutes by design), so full replay-from-scratch on resume is
  cheap enough that snapshotting would be premature complexity.
- **WAL-mode SQLite / Postgres `LISTEN/NOTIFY` for streaming** instead of
  polling. Polling at 500ms is simple, debuggable, and fast enough for a
  human-paced agent loop (tool calls take seconds, not milliseconds); we'd
  reach for `LISTEN/NOTIFY` only if sub-second latency mattered, which it
  doesn't for this product surface.

## Consequences
- Every new side-effecting step type the orchestrator gains in later
  phases must journal before/atomically-with the effect, or it breaks the
  resumability guarantee silently for just that step type.
- The journal grows monotonically and is never compacted in this design;
  for a long-lived task list this would need a retention policy (covered
  in DESIGN.md's scale section), not addressed in Phase 1.
