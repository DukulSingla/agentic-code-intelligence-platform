from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_user
from app.models import User, Workspace, get_db
from app.retrieval.workspace import GitError, init_canonical_repo
from app.schemas import WorkspaceCreate, WorkspaceOut

router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])

_REMOTE_SOURCE_PREFIXES = ("http://", "https://", "git@", "ssh://")


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
    source = body.source.strip()
    is_remote = source.startswith(_REMOTE_SOURCE_PREFIXES)

    # Local sources are validated up front, before we touch the DB at all --
    # a bad local path should fail fast with a clear 400, not after we've
    # already inserted a workspace row. Remote URLs skip this: there's no
    # cheap, reliable way to validate a git URL without attempting the clone
    # itself, so for those the clone attempt below is the validation.
    if not is_remote:
        source_path = Path(source)
        if not source_path.exists():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"path does not exist: {source}")
        if not (source_path / ".git").exists():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"not a git repository: {source}")

    ws = Workspace(user_id=user.id, name=body.name, repo_path="", default_branch=body.default_branch)
    db.add(ws)
    await db.flush()  # assign ws.id before we use it as the on-disk dir name

    try:
        repo = init_canonical_repo(ws.id, source, body.default_branch)
        ws.repo_path = str(repo.repo_path)
        await db.commit()
        await db.refresh(ws)
        return ws
    except GitError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"failed to clone repository: {e}")
    except Exception:
        await db.rollback()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="failed to create workspace")


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
