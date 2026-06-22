# from pathlib import Path

# from fastapi import APIRouter, Depends, HTTPException, status
# from sqlalchemy import select
# from sqlalchemy.ext.asyncio import AsyncSession

# from app.auth import require_user
# from app.models import User, Workspace, get_db
# from app.retrieval.workspace import init_canonical_repo, GitError
# from app.schemas import WorkspaceCreate, WorkspaceOut

# router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])


# async def get_owned_workspace(workspace_id: str, user: User, db: AsyncSession) -> Workspace:
#     result = await db.execute(
#         select(Workspace).where(
#             Workspace.id == workspace_id,
#             Workspace.user_id == user.id,
#         )
#     )
#     ws = result.scalar_one_or_none()

#     if ws is None:
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail="workspace not found",
#         )

#     return ws


# @router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
# async def create_workspace(
#     body: WorkspaceCreate,
#     user: User = Depends(require_user),
#     db: AsyncSession = Depends(get_db),
# ) -> Workspace:

#     source_path = Path(body.source)

#     # Validate source exists
#     if not source_path.exists():
#         raise HTTPException(
#             status_code=status.HTTP_400_BAD_REQUEST,
#             detail=f"Repository path does not exist: {body.source}",
#         )

#     # Validate it's a git repo
#     if not (source_path / ".git").exists():
#         raise HTTPException(
#             status_code=status.HTTP_400_BAD_REQUEST,
#             detail=f"Path is not a git repository: {body.source}",
#         )

#     ws = Workspace(
#         user_id=user.id,
#         name=body.name,
#         repo_path="",
#         default_branch=body.default_branch,
#     )

#     db.add(ws)
#     await db.flush()

#     try:
#         repo = init_canonical_repo(
#             ws.id,
#             body.source,
#             body.default_branch,
#         )

#         ws.repo_path = str(repo.repo_path)

#         await db.commit()

#     except GitError as e:
#         await db.rollback()

#         raise HTTPException(
#             status_code=status.HTTP_400_BAD_REQUEST,
#             detail=f"Failed to initialize repository: {str(e)}",
#         )

#     except Exception:
#         await db.rollback()

#         raise HTTPException(
#             status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
#             detail="Failed to create workspace",
#         )

#     return ws 

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


async def get_owned_workspace(
    workspace_id: str,
    user: User,
    db: AsyncSession,
) -> Workspace:
    """
    Fetch a workspace scoped to the authenticated user.

    Returns 404 for:
      - workspace does not exist
      - workspace exists but belongs to another user

    This prevents workspace enumeration attacks.
    """
    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.user_id == user.id,
        )
    )

    ws = result.scalar_one_or_none()

    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workspace not found",
        )

    return ws


@router.post(
    "",
    response_model=WorkspaceOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_workspace(
    body: WorkspaceCreate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Workspace:
    source = body.source.strip()

    # ------------------------------------------------------------------
    # Support:
    #   - Local repositories
    #   - GitHub HTTPS URLs
    #   - SSH Git URLs
    # ------------------------------------------------------------------
    is_remote_repo = any(
        source.startswith(prefix)
        for prefix in (
            "http://",
            "https://",
            "git@",
            "ssh://",
        )
    )

    # ------------------------------------------------------------------
    # Validate local repositories
    # ------------------------------------------------------------------
    if not is_remote_repo:
        source_path = Path(source)

        if not source_path.exists():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Repository path does not exist: {source}",
            )

        if not (source_path / ".git").exists():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Path is not a git repository: {source}",
            )

    ws = Workspace(
        user_id=user.id,
        name=body.name,
        repo_path="",
        default_branch=body.default_branch,
    )

    db.add(ws)
    await db.flush()  # obtain workspace id

    try:
        repo = init_canonical_repo(
            ws.id,
            source,
            body.default_branch,
        )

        ws.repo_path = str(repo.repo_path)

        await db.commit()
        await db.refresh(ws)

        return ws

    except GitError:
        await db.rollback()

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to clone or initialize repository",
        )

    except Exception:
        await db.rollback()

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create workspace",
        )


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[Workspace]:
    result = await db.execute(
        select(Workspace).where(
            Workspace.user_id == user.id,
        )
    )

    return list(result.scalars().all())


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(
    workspace_id: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Workspace:
    return await get_owned_workspace(
        workspace_id,
        user,
        db,
    )