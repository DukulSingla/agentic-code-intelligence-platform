"""
Schema (Postgres-ready, run on SQLite for local dev):

  users            — one row per API consumer
  workspaces       — a repo owned by a user
  tasks            — one agent run against a workspace
  journal_events   — append-only event log per task (crash-resumability)
  task_checkpoints — coarse-grained resume snapshots per task
  budget_ledger    — append-only metering entries per task
  symbols          — tree-sitter indexed symbols per workspace (Phase 2)
  file_index_state — per-file content hash so re-indexing is incremental

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


class TaskCheckpoint(Base):
    """
    Coarse-grained resume points — distinct from journal_events on purpose.

    journal_events is the fine-grained, append-only proof log: one row per
    model call, tool call, patch, or state transition. Replaying it from
    step 1 after a crash is *correct*, but on a long-running task it's slow:
    every already-completed model call would need to be read back row by
    row before the orchestrator even reaches the point it actually needs to
    resume from, and the orchestrator would have to reconstruct in-memory
    agent state (running budget counters, the current plan, the message
    history sent to the model) by re-deriving it from raw events instead of
    just having it.

    A checkpoint is a periodic snapshot the orchestrator writes at safe
    points (e.g. after each completed loop iteration) containing: which
    journal step_index it's caught up to, the task's TaskState at that
    point, and a serialized snapshot of whatever in-memory context resume
    needs. On resume: load the latest checkpoint for the task (if any),
    replay only journal_events with step_index > checkpoint.step_index, and
    rehydrate agent state from context_snapshot instead of from scratch.
    No checkpoint yet -> fall back to full journal replay from step 0,
    which is always correct, just slower.
    """
    __tablename__ = "task_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String, ForeignKey("tasks.id"), nullable=False, index=True)
    # The journal step_index this checkpoint is caught up to. Resume replays
    # only journal_events with step_index > this value.
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[TaskState] = mapped_column(Enum(TaskState), nullable=False)
    # Whatever the orchestrator needs to rehydrate without re-deriving it
    # from raw journal rows: plan, conversation history, running counters.
    context_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("task_id", "step_index", name="uq_checkpoint_task_step"),
        Index("ix_checkpoint_task_step", "task_id", "step_index"),
    )


class SymbolKind(str, enum.Enum):
    function = "function"
    class_ = "class"
    method = "method"


class Symbol(Base):
    """
    One row per named symbol extracted by the tree-sitter indexer.

    Scoped to workspace_id (not task_id) because the symbol index is a
    property of the repo content, not of any individual task run. Two tasks
    on the same workspace share the same symbol index — each task reads from
    their own worktree snapshot, but the index is built from the canonical
    repo and treated as valid until a file's content_hash changes.

    parent_name is set for methods (their containing class name) and None
    for top-level functions and classes. This lets `definition("MyClass.my_method")`
    resolve via a (workspace_id, name, parent_name) lookup without a JOIN.
    """
    __tablename__ = "symbols"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String, ForeignKey("workspaces.id"), nullable=False)
    file_path: Mapped[str] = mapped_column(String, nullable=False)   # relative to repo root
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[SymbolKind] = mapped_column(Enum(SymbolKind), nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based, inclusive
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)    # 1-based, inclusive
    parent_name: Mapped[str | None] = mapped_column(String, nullable=True)  # class name for methods

    __table_args__ = (
        # Fast symbol-name lookup (the most common query pattern)
        Index("ix_symbol_workspace_name", "workspace_id", "name"),
        # Fast "all symbols in file X" lookup (needed by the indexer on re-index)
        Index("ix_symbol_workspace_file", "workspace_id", "file_path"),
    )


class FileIndexState(Base):
    """
    Tracks what content_hash we last indexed for each file in a workspace.
    The indexer checks this before parsing: if the file's current SHA256
    matches the stored hash the file is up-to-date and is skipped, making
    re-indexing incremental rather than a full re-parse every time.

    On re-index of a changed file: delete all Symbol rows for that
    (workspace_id, file_path), re-parse, insert fresh Symbol rows, update
    this row's hash. Unique on (workspace_id, file_path) so UPSERT is safe.
    """
    __tablename__ = "file_index_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String, ForeignKey("workspaces.id"), nullable=False)
    file_path: Mapped[str] = mapped_column(String, nullable=False)   # relative to repo root
    content_hash: Mapped[str] = mapped_column(String, nullable=False)  # SHA256 hex
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("workspace_id", "file_path", name="uq_file_index_ws_path"),
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
