from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_user
from app.models import User, Workspace, get_db
from app.retrieval.workspace import init_canonical_repo
from app.schemas import WorkspaceCreate, WorkspaceOut

router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])


async def get_owned_workspace(workspace_id: str, user: User, db: AsyncSession) -> Workspace:
    """
    The one and only way a workspace is fetched anywhere in this codebase.
    Scoped by user_id in the WHERE clause itself (not filtered after the
    fact), and returns 404 for "doesn't exist" and "exists but isn't yours"
    alike — we deliberately do not distinguish the two to a caller, so the
    API can't be used to enumerate other users' workspace ids.
    """
    result = await db.execute(
        select(Workspace).where(Workspace.id == workspace_id, Workspace.user_id == user.id)
    )
    ws = result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found")
    return ws


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceCreate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Workspace:
    ws = Workspace(user_id=user.id, name=body.name, repo_path="", default_branch=body.default_branch)
    db.add(ws)
    await db.flush()  # assign ws.id before we use it as the on-disk dir name

    repo = init_canonical_repo(ws.id, body.source, body.default_branch)
    ws.repo_path = str(repo.repo_path)

    await db.commit()
    return ws


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[Workspace]:
    result = await db.execute(select(Workspace).where(Workspace.user_id == user.id))
    return list(result.scalars().all())


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(
    workspace_id: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Workspace:
    return await get_owned_workspace(workspace_id, user, db)
