"""
Per-task workspace isolation via `git worktree`.

Why worktrees over a full clone-per-task:
  - Shares the .git object store with the canonical repo (cheap: a worktree
    add on a 1M-LOC repo is a checkout, not a clone — no network, no full
    object copy).
  - Each worktree is a real, independent working directory: a task can run
    `pytest`, write files, `git diff`, etc. without any other task observing
    its uncommitted state. That's the "isolated, consistent view" the
    assignment requires (§2.4).
  - Two tasks on the same workspace get two worktrees on two branches
    (`task/<task_id>`), so concurrent edits can never collide on disk. The
    only place they CAN collide is at merge-back time into the workspace's
    base branch, which is exactly where we want explicit conflict handling
    rather than a silent clobber.

This module owns: creating the canonical clone for a workspace, creating and
removing per-task worktrees, and merging a task's branch back into base with
explicit conflict detection. It does not call out to the network beyond the
initial clone.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import settings


class GitError(RuntimeError):
    pass


class WorkspaceConflictError(GitError):
    """Raised when a task's branch cannot be cleanly merged into base."""

    def __init__(self, conflicting_files: list[str]):
        self.conflicting_files = conflicting_files
        super().__init__(f"merge conflict in: {', '.join(conflicting_files)}")


def _run(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise GitError(f"`{' '.join(args)}` failed in {cwd}:\n{proc.stderr.strip()}")
    return proc.stdout.strip()


@dataclass
class WorkspaceRepo:
    workspace_id: str
    repo_path: Path
    default_branch: str = "main"


def init_canonical_repo(workspace_id: str, source: str, default_branch: str = "main") -> WorkspaceRepo:
    """
    Materialize the canonical repo for a workspace under repos_dir.
    `source` may be a local path (sample/synthetic repos, per the
    assignment's "synthetic repos only" rule) or a git URL.
    Idempotent: if the repo already exists on disk, reuse it.
    """
    repo_path = settings.repos_dir / workspace_id
    if repo_path.exists():
        return WorkspaceRepo(workspace_id, repo_path, default_branch)

    repo_path.parent.mkdir(parents=True, exist_ok=True)
    source_path = Path(source)
    if source_path.exists():
        # Local source: clone so the canonical repo is independent of the
        # original directory (workspace_isolation also means "isolated from
        # the source the user pointed us at", not just from other users).
        _run(["git", "clone", "--no-hardlinks", str(source_path), str(repo_path)], cwd=repo_path.parent)
    else:
        _run(["git", "clone", source, str(repo_path)], cwd=repo_path.parent)

    # Normalize branch name and ensure committer identity exists for this repo
    # (sample/CI environments often lack a global git user.email).
    _run(["git", "config", "user.email", "agent@sarvam-code-intel.local"], cwd=repo_path)
    _run(["git", "config", "user.name", "Sarvam Code Intel Agent"], cwd=repo_path)
    current = _run(["git", "branch", "--show-current"], cwd=repo_path)
    if current != default_branch:
        _run(["git", "branch", "-m", current, default_branch], cwd=repo_path)

    return WorkspaceRepo(workspace_id, repo_path, default_branch)


def create_worktree(repo: WorkspaceRepo, task_id: str) -> Path:
    """
    Create an isolated worktree + branch for a single task.
    Branch name `task/<task_id>` so it's unambiguous in `git branch -a`
    and never collides with a user-named branch.
    """
    worktree_path = settings.worktrees_dir / task_id
    if worktree_path.exists():
        # Resuming a crashed task that already has a worktree: reuse it.
        return worktree_path

    branch = f"task/{task_id}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path), repo.default_branch],
        cwd=repo.repo_path,
    )
    return worktree_path


def remove_worktree(repo: WorkspaceRepo, task_id: str, *, delete_branch: bool = True) -> None:
    """Tear down a task's worktree. Safe to call on an already-removed worktree."""
    worktree_path = settings.worktrees_dir / task_id
    branch = f"task/{task_id}"

    if worktree_path.exists():
        try:
            _run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=repo.repo_path)
        except GitError:
            # Worktree metadata can get out of sync with the filesystem
            # (e.g. the process was killed mid-checkout). Fall back to a
            # raw rmtree + prune so cleanup is never blocked.
            shutil.rmtree(worktree_path, ignore_errors=True)
            _run(["git", "worktree", "prune"], cwd=repo.repo_path)

    if delete_branch:
        try:
            _run(["git", "branch", "-D", branch], cwd=repo.repo_path)
        except GitError:
            pass  # branch may not exist if the task never made a commit


def diff_against_base(repo: WorkspaceRepo, task_id: str) -> str:
    """Unified diff of everything the task has changed, relative to base."""
    worktree_path = settings.worktrees_dir / task_id
    return _run(["git", "diff", repo.default_branch], cwd=worktree_path)


def merge_into_base(repo: WorkspaceRepo, task_id: str) -> None:
    """
    Fast-forward-or-merge a verified task branch into the workspace's base
    branch. Used only in `mode=apply` after verification has passed.

    Concurrency contract: this is the single point of contention between
    tasks on the same workspace. We serialize it with a filesystem lock on
    the repo path so two "verified, about to merge" tasks can't race; the
    second one to acquire the lock either fast-forwards cleanly or fails
    with WorkspaceConflictError — it is NEVER silently overwritten or
    silently dropped.
    """
    branch = f"task/{task_id}"
    lock_path = repo.repo_path.with_suffix(".merge.lock")
    import filelock  # local import: only needed on the merge path

    with filelock.FileLock(str(lock_path), timeout=30):
        _run(["git", "checkout", repo.default_branch], cwd=repo.repo_path)
        try:
            _run(["git", "merge", "--no-ff", branch, "-m", f"Merge {branch}"], cwd=repo.repo_path)
        except GitError as e:
            conflicted = _run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo.repo_path)
            _run(["git", "merge", "--abort"], cwd=repo.repo_path)
            files = [f for f in conflicted.splitlines() if f]
            raise WorkspaceConflictError(files) from e
