"""
Schema (Postgres-ready, run on SQLite for local dev):

  users            — one row per API consumer
  workspaces       — a repo owned by a user
  tasks            — one agent run against a workspace
  journal_events   — append-only event log per task (crash-resumability)
  budget_ledger    — append-only metering entries per task

Isolation is enforced at the query layer (see app/auth.py and the
`*_for_user` helpers below): every lookup of a workspace or task is scoped
by user_id. There is no code path that fetches a workspace/task by id alone.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    """e.g. new_id('usr') -> 'usr_3f9c1a2b9e4d'"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Base(AsyncAttrs, DeclarativeBase):
    pass


class TaskMode(str, enum.Enum):
    apply = "apply"
    dry_run = "dry_run"


class TaskState(str, enum.Enum):
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    RETRIEVING = "RETRIEVING"
    EDITING = "EDITING"
    VERIFYING = "VERIFYING"
    REPAIRING = "REPAIRING"
    DONE = "DONE"                       # terminal: verified-success
    FAILED = "FAILED"                   # terminal: give-up-with-reason
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"  # terminal
    CANCELLING = "CANCELLING"           # cancel requested; orchestrator stops at next checkpoint
    CANCELLED = "CANCELLED"             # terminal: user-requested stop, honored cleanly

    @property
    def is_terminal(self) -> bool:
        return self in (TaskState.DONE, TaskState.FAILED, TaskState.BUDGET_EXHAUSTED, TaskState.CANCELLED)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("usr"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("ws"))
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Path to the canonical (bare or main) clone on disk. Per-task worktrees
    # are created from this and never mutate it directly.
    repo_path: Mapped[str] = mapped_column(String, nullable=False)
    default_branch: Mapped[str] = mapped_column(String, default="main")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_workspace_user_name"),)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("tsk"))
    # Denormalized user_id (not just via workspace) so every isolation check
    # is a single indexed WHERE clause with no join required.
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(String, ForeignKey("workspaces.id"), nullable=False, index=True)

    instruction: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[TaskMode] = mapped_column(Enum(TaskMode), default=TaskMode.apply)

    budget_max_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    budget_max_usd: Mapped[float] = mapped_column(Float, nullable=False)
    budget_max_wall_seconds: Mapped[int] = mapped_column(Integer, nullable=False)

    state: Mapped[TaskState] = mapped_column(Enum(TaskState), default=TaskState.QUEUED, index=True)
    # Path to this task's isolated git worktree (created lazily on first run).
    worktree_path: Mapped[str | None] = mapped_column(String, nullable=True)

    # Idempotency: client-supplied key, optional. Prevents duplicate task
    # creation on client-side retry of the POST.
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    result_patch: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_task_user_idempotency"),
    )


class JournalEvent(Base):
    """
    Append-only. One row per (task_id, step_index). The orchestrator writes
    this row BEFORE executing the side effect it describes, and replay logic
    treats "row exists" as "this step already happened — do not redo it."
    """
    __tablename__ = "journal_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String, ForeignKey("tasks.id"), nullable=False, index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)  # model_call|tool_call|patch|verify|state_transition
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("task_id", "step_index", name="uq_journal_task_step"),
        Index("ix_journal_task_step", "task_id", "step_index"),
    )


class BudgetLedgerEntry(Base):
    """
    Append-only metering log. The running total per (task, dimension) is a
    SUM over this table, never a mutable counter — so it survives crashes
    and is auditable after the fact.
    """
    __tablename__ = "budget_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String, ForeignKey("tasks.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False, index=True)
    dimension: Mapped[str] = mapped_column(String, nullable=False)  # tokens_in|tokens_out|tool_calls|retrieval_bytes|sandbox_seconds|usd
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# --- Engine / session plumbing -------------------------------------------------

engine = create_async_engine(settings.database_url, future=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    settings.ensure_dirs()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
