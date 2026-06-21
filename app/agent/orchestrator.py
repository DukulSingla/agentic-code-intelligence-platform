"""
Agent orchestrator — Phase 3.

This module is intentionally a stub for Phase 1. The API layer already
treats `run_task` as the single entrypoint that drives a task from QUEUED
to a terminal state (DONE / FAILED / BUDGET_EXHAUSTED), so wiring in the
real ReAct loop later is a drop-in replacement with no change to the API,
journal, or budget code.

Phase 3 will implement here:
  - the bounded plan -> retrieve -> edit -> verify -> repair loop
  - journal-replay-based resume (skip any step_index already in the journal)
  - LLM gateway calls metered against the task's budget
  - a checkpoint at each loop iteration that checks task.state for
    TaskState.CANCELLING (set by POST /v1/tasks/{id}/cancel) using the
    exact same code path as the budget-exhaustion check, and on either
    finalizes to TaskState.CANCELLED or TaskState.BUDGET_EXHAUSTED — never
    leaving a task silently stuck in an in-flight state
"""
from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AsyncSessionLocal, JournalEvent, Task, TaskState

log = structlog.get_logger()


async def _append_journal(db: AsyncSession, task_id: str, step_index: int, event_type: str, payload: dict) -> None:
    db.add(JournalEvent(task_id=task_id, step_index=step_index, event_type=event_type, payload=payload))
    await db.commit()


async def run_task(task_id: str) -> None:
    """
    Background entrypoint scheduled by POST /v1/tasks. Owns its own DB
    session since it outlives the request that triggered it.

    Phase 1 behavior: journal that the task was accepted and leave it in
    QUEUED. We do not fabricate a DONE/FAILED result — an honest "not yet
    implemented" terminal state does not exist in this system, so Phase 1
    tasks simply stay observably QUEUED rather than claim a result they
    didn't earn.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task is None:
            log.warning("orchestrator.task_not_found", task_id=task_id)
            return

        await _append_journal(
            db, task_id, step_index=1, event_type="state_transition",
            payload={"from": "QUEUED", "to": "QUEUED", "note": "orchestrator loop lands in Phase 3"},
        )
        log.info("orchestrator.accepted", task_id=task_id, state=task.state.value)
