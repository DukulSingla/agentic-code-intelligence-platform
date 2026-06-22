"""
Pure file I/O for the retrieval layer.

This module knows nothing about budgets, tasks, or the agent loop.
It has exactly one job: safely read files from a worktree.

Dependency graph:
    tools.py -> reader.py
    indexer.py -> reader.py
    reader.py -> nothing
"""

from __future__ import annotations

import hashlib
from pathlib import Path


MAX_FILE_BYTES = 5_000_000  # 5 MB

IGNORE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "data"
}


class ReaderError(ValueError):
    """Raised when a read request is invalid."""


def _resolve_file(
    worktree_path: Path,
    rel_file_path: str,
) -> Path:
    """
    Resolve a repository-relative path safely.

    Prevents:
      - path traversal (../../etc/passwd)
      - absolute path escapes
      - symlink escapes
      - oversized file reads
    """
    abs_path = (worktree_path / rel_file_path).resolve()
    worktree_resolved = worktree_path.resolve()

    try:
        abs_path.relative_to(worktree_resolved)
    except ValueError:
        raise ReaderError(
            f"path traversal rejected: {rel_file_path!r}"
        )

    if not abs_path.exists():
        raise ReaderError(
            f"file not found: {rel_file_path}"
        )

    if not abs_path.is_file():
        raise ReaderError(
            f"not a file: {rel_file_path}"
        )

    if abs_path.stat().st_size > MAX_FILE_BYTES:
        raise ReaderError(
            f"file exceeds maximum allowed size "
            f"({MAX_FILE_BYTES:,} bytes): {rel_file_path}"
        )

    return abs_path

def read_bytes(
    worktree_path: Path,
    rel_file_path: str,
) -> bytes:
    """
    Read raw file bytes.

    Used by:
      - references()
      - structural_grep()
      - future indexer implementations
    """
    abs_path = _resolve_file(
        worktree_path,
        rel_file_path,
    )

    return abs_path.read_bytes()


def read_span(
    worktree_path: Path,
    rel_file_path: str,
    start_line: int,
    end_line: int,
) -> str:
    """
    Read lines [start_line, end_line] inclusive.

    Lines are 1-indexed.

    Examples:
        read_span("foo.py", 1, 10)
        read_span("foo.py", 50, 99999)  # returns 50 -> EOF
    """
    if start_line < 1:
        raise ReaderError(
            f"start_line must be >= 1, got {start_line}"
        )

    if end_line < start_line:
        raise ReaderError(
            f"end_line ({end_line}) must be >= start_line ({start_line})"
        )

    abs_path = _resolve_file(
        worktree_path,
        rel_file_path,
    )

    lines = abs_path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines(keepends=True)

    total_lines = len(lines)

    if total_lines == 0:
        return ""

    if start_line > total_lines:
        raise ReaderError(
            f"start_line {start_line} is beyond EOF "
            f"({total_lines} lines): {rel_file_path}"
        )

    end_clamped = min(
        end_line,
        total_lines,
    )

    return "".join(
        lines[start_line - 1 : end_clamped]
    )


def read_file(
    worktree_path: Path,
    rel_file_path: str,
) -> str:
    """
    Read an entire file.

    Intended for internal retrieval components such as:
      - indexer
      - references
      - structural_grep

    Not exposed directly as an agent-facing tool.
    """
    abs_path = _resolve_file(
        worktree_path,
        rel_file_path,
    )

    return abs_path.read_text(
        encoding="utf-8",
        errors="replace",
    )


def content_hash(
    worktree_path: Path,
    rel_file_path: str,
) -> str:
    """
    SHA256 hash of a file's raw bytes.

    Used by the indexer to determine whether
    a file needs re-indexing.
    """
    abs_path = _resolve_file(
        worktree_path,
        rel_file_path,
    )

    return hashlib.sha256(
        abs_path.read_bytes()
    ).hexdigest()


def list_python_files(
    root: Path,
) -> list[str]:
    """
    Return all Python files beneath root as
    root-relative paths.

    Excludes:
      - .git
      - .venv
      - venv
      - node_modules
      - __pycache__
    """
    result: list[str] = []

    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)

        if any(
            part in IGNORE_DIRS
            for part in relative.parts
        ):
            continue

        result.append(
            str(relative)
        )

    return result

def resolve_directory(
    worktree_path: Path,
    rel_path: str,
) -> Path:
    abs_path = (
        worktree_path / rel_path
    ).resolve()

    worktree_resolved = worktree_path.resolve()

    try:
        abs_path.relative_to(
            worktree_resolved
        )
    except ValueError:
        raise ReaderError(
            f"path traversal rejected: {rel_path!r}"
        )

    if not abs_path.exists():
        raise ReaderError(
            f"path not found: {rel_path}"
        )

    if not abs_path.is_dir():
        raise ReaderError(
            f"not a directory: {rel_path}"
        )

    return abs_path