from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models import TaskMode, TaskState


# --- Workspaces -----------------------------------------------------------

class WorkspaceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    source: str = Field(..., description="Local path or git URL to a synthetic/sample repo")
    default_branch: str = "main"


class WorkspaceOut(BaseModel):
    id: str
    name: str
    default_branch: str
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Tasks ------------------------------------------------------------------

class Budget(BaseModel):
    max_tokens: int = Field(gt=0)
    max_usd: float = Field(gt=0)
    max_wall_seconds: int = Field(gt=0)


class TaskCreate(BaseModel):
    workspace_id: str
    instruction: str = Field(..., min_length=1)
    budget: Budget | None = None
    mode: TaskMode = TaskMode.apply
    idempotency_key: str | None = Field(
        default=None,
        description="Optional client-supplied key. Re-POSTing the same key "
        "for this user returns the existing task instead of creating a new one.",
    )


class TaskAccepted(BaseModel):
    task_id: str
    events: str


class TaskOut(BaseModel):
    id: str
    workspace_id: str
    instruction: str
    mode: TaskMode
    state: TaskState
    budget_max_tokens: int
    budget_max_usd: float
    budget_max_wall_seconds: int
    result_patch: str | None
    failure_reason: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class JournalEventOut(BaseModel):
    step_index: int
    event_type: str
    payload: dict
    created_at: datetime

    model_config = {"from_attributes": True}
