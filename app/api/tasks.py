from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.agent.orchestrator import run_task
from app.auth import require_user
from app.config import settings
from app.models import JournalEvent, Task, TaskState, User, get_db
from app.retrieval.workspace import WorkspaceRepo, create_worktree
from app.schemas import JournalEventOut, TaskAccepted, TaskCreate, TaskOut
from app.api.workspaces import get_owned_workspace

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


async def get_owned_task(task_id: str, user: User, db: AsyncSession) -> Task:
    """Single fetch path for tasks, scoped by user_id — see get_owned_workspace for rationale."""
    result = await db.execute(select(Task).where(Task.id == task_id, Task.user_id == user.id))
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    return task


@router.post("", response_model=TaskAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_task(
    body: TaskCreate,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> TaskAccepted:
    # Idempotency: re-POSTing the same (user, idempotency_key) returns the
    # existing task rather than starting a duplicate paid run. Caught at the
    # DB unique constraint too, but we check first for a friendlier path.
    if body.idempotency_key:
        existing = await db.execute(
            select(Task).where(Task.user_id == user.id, Task.idempotency_key == body.idempotency_key)
        )
        existing_task = existing.scalar_one_or_none()
        if existing_task is not None:
            return TaskAccepted(task_id=existing_task.id, events=f"/v1/tasks/{existing_task.id}/events")

    ws = await get_owned_workspace(body.workspace_id, user, db)

    budget = body.budget or type(
        "B", (), {
            "max_tokens": settings.default_max_tokens,
            "max_usd": settings.default_max_usd,
            "max_wall_seconds": settings.default_max_wall_seconds,
        },
    )()

    task = Task(
        user_id=user.id,
        workspace_id=ws.id,
        instruction=body.instruction,
        mode=body.mode,
        budget_max_tokens=budget.max_tokens,
        budget_max_usd=budget.max_usd,
        budget_max_wall_seconds=budget.max_wall_seconds,
        idempotency_key=body.idempotency_key,
    )
    db.add(task)
    await db.flush()

    db.add(JournalEvent(
        task_id=task.id, step_index=0, event_type="state_transition",
        payload={"from": None, "to": "QUEUED"},
    ))
    await db.commit()

    # Worktree creation is cheap (a checkout, not a clone) and happens
    # synchronously so a 202 response guarantees the task's isolated
    # workspace already exists on disk.
    repo = WorkspaceRepo(ws.id, Path(ws.repo_path), ws.default_branch)
    worktree_path = create_worktree(repo, task.id)
    task.worktree_path = str(worktree_path)
    await db.commit()

    background_tasks.add_task(run_task, task.id)

    return TaskAccepted(task_id=task.id, events=f"/v1/tasks/{task.id}/events")


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Task:
    return await get_owned_task(task_id, user, db)


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[Task]:
    result = await db.execute(select(Task).where(Task.user_id == user.id).order_by(Task.created_at.desc()))
    return list(result.scalars().all())


@router.get("/{task_id}/events")
async def stream_task_events(
    task_id: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """
    SSE stream of journal events for a task, from the beginning. Polls the
    journal table rather than holding an in-process pubsub channel, so a
    client can disconnect and reconnect (or a second client can attach to
    the same task) and always sees the full history — the journal IS the
    source of truth, the stream is just a read-replica view of it.
    """
    await get_owned_task(task_id, user, db)  # 404s early if not owned, before opening the stream

    async def event_generator():
        last_seen = -1
        from app.models import AsyncSessionLocal  # local import: avoid a module-level cycle

        while True:
            async with AsyncSessionLocal() as poll_db:
                result = await poll_db.execute(
                    select(JournalEvent)
                    .where(JournalEvent.task_id == task_id, JournalEvent.step_index > last_seen)
                    .order_by(JournalEvent.step_index)
                )
                events = result.scalars().all()
                for ev in events:
                    last_seen = ev.step_index
                    yield {
                        "event": ev.event_type,
                        "data": json.dumps({
                            "step_index": ev.step_index,
                            "payload": ev.payload,
                            "created_at": ev.created_at.isoformat(),
                        }),
                    }

                task_result = await poll_db.execute(select(Task).where(Task.id == task_id))
                task = task_result.scalar_one()
                if task.state.is_terminal:
                    yield {"event": "terminal", "data": json.dumps({"state": task.state.value})}
                    return

            await asyncio.sleep(0.5)

    return EventSourceResponse(event_generator())


@router.get("/{task_id}/journal", response_model=list[JournalEventOut])
async def get_task_journal(
    task_id: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[JournalEvent]:
    """
    Full, synchronous dump of the append-only journal for one task, in
    step_index order.

    This is deliberately a separate endpoint from /events: the SSE stream
    is a live tail meant for a connected caller watching a run progress,
    while this is the "give me everything you recorded" view the eval
    harness and replay tooling need — read once, parse, assert on it,
    without holding a stream open or worrying about reconnect semantics.
    It is also the only way to audit a finished run after the fact: what
    every model call, tool call, and patch actually was, in order.
    """
    await get_owned_task(task_id, user, db)  # ownership check before reading any journal rows
    result = await db.execute(
        select(JournalEvent).where(JournalEvent.task_id == task_id).order_by(JournalEvent.step_index)
    )
    return list(result.scalars().all())


@router.post("/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(
    task_id: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Task:
    """
    User-initiated cancellation. Not required by the spec's failure-scenario
    list, but it demonstrates the same "stop cleanly, never corrupt the
    workspace" guarantee budget-exhaustion gives us, on demand rather than
    waiting for a budget to run out.

    Two cases, matched to what's actually true about the task right now:

    - QUEUED: nothing is executing yet (Phase 1's orchestrator stub returns
      immediately; Phase 3's real loop hasn't claimed the task off the
      queue). It is safe to finalize straight to CANCELLED — there is no
      in-flight side effect to interrupt.
    - Any in-flight loop state (PLANNING/RETRIEVING/EDITING/VERIFYING/
      REPAIRING): we do NOT touch task.state to CANCELLED directly, because
      the orchestrator may be mid-step (e.g. mid-sandbox-run). We set
      CANCELLING and journal it; the orchestrator's loop (Phase 3) checks
      for CANCELLING at the same checkpoint it already checks budget
      exhaustion, and is the only writer that ever moves a task into the
      terminal CANCELLED state from there. This mirrors exactly how budget
      exhaustion is handled, so cancellation needs no separate enforcement
      path to reason about.

    Already-terminal tasks return 409: cancellation of a finished task is
    a no-op we refuse rather than silently accept, since the caller's
    mental model of "did my cancel do anything" should be reliable.
    """
    task = await get_owned_task(task_id, user, db)

    if task.state.is_terminal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"task already in terminal state {task.state.value}; cannot cancel",
        )

    previous_state = task.state
    next_state = TaskState.CANCELLED if task.state == TaskState.QUEUED else TaskState.CANCELLING
    task.state = next_state
    if next_state == TaskState.CANCELLED:
        task.completed_at = datetime.now(timezone.utc)
        task.failure_reason = "cancelled by user before execution started"

    next_step = await db.execute(
        select(JournalEvent.step_index).where(JournalEvent.task_id == task_id).order_by(JournalEvent.step_index.desc())
    )
    last_step = next_step.scalar_one_or_none() or 0

    db.add(JournalEvent(
        task_id=task_id, step_index=last_step + 1, event_type="state_transition",
        payload={"from": previous_state.value, "to": next_state.value, "reason": "user_requested_cancel"},
    ))
    await db.commit()
    return task
