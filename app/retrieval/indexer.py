"""
Tree-sitter indexer: walks a worktree, extracts Python symbols (functions,
classes, methods) with exact line spans, and stores them in the `symbols`
table for fast structural lookup.

Design decisions:
- Incremental: each file's SHA256 is stored in file_index_state. On
  re-index, only files whose hash changed are re-parsed. A fresh index of
  an already-indexed workspace with no changes is a no-op.
- Synchronous: indexing runs in a background thread (called from the async
  orchestrator via asyncio.to_thread). Tree-sitter's Python bindings are
  not async-native and the index runs once per task start, not per query.
- Workspace-scoped, not task-scoped: the index is a property of the repo
  content, shared across tasks on the same workspace. Each task passes its
  own worktree_path so the index always reflects the task's isolated snapshot.
- Python-only for Phase 2 (scope cut documented in DESIGN.md §4).
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import tree_sitter_python as tspython
from sqlalchemy import delete, select
# from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from tree_sitter import Language, Parser

from app.models import AsyncSessionLocal, FileIndexState, Symbol, SymbolKind
from app.retrieval.reader import content_hash, list_python_files, read_bytes

# One parser instance per thread.
# tree-sitter Parser is not thread-safe, so we use thread-local storage.
# asyncio.to_thread uses a ThreadPoolExecutor that reuses threads,
# giving us parser reuse without cross-thread races.
_PY_LANGUAGE = Language(tspython.language())

# threading.local() — same reasoning as tools.py: asyncio.to_thread uses a
# ThreadPoolExecutor that reuses threads, so thread-local gives each thread
# its own Parser instance without construction overhead on every call and
# without the race condition a single global Parser would create under
# concurrent tasks. See tools.py for the full explanation.
_local = threading.local()


def _get_parser() -> Parser:
    if not hasattr(_local, "parser"):
        _local.parser = Parser(_PY_LANGUAGE)
    return _local.parser


# ---------------------------------------------------------------------------
# AST walking
# ---------------------------------------------------------------------------

def _extract_symbols(source: bytes, rel_path: str) -> list[dict]:
    """
    Parse `source` and return a list of symbol dicts ready for DB insert.
    Each dict has: file_path, name, kind, start_line, end_line, parent_name.
    Lines are 1-based inclusive.
    """
    tree = _get_parser().parse(source)
    symbols: list[dict] = []
    _walk(tree.root_node, rel_path, parent_class=None, out=symbols)
    return symbols


def _walk(node, rel_path: str, parent_class: str | None, out: list[dict]) -> None:
    """
    Recursively walk the AST. We care about three node types:
      - class_definition            -> kind=class, recurse with parent_class set
      - function_definition         -> kind=function (top-level) or method (inside class)
      - decorated_definition        -> unwrap to get the inner function/class

    tree-sitter represents `@decorator\ndef f(): ...` as a decorated_definition
    whose child is the function_definition. We unwrap the decorator layer so
    the symbol's line span covers the decorator too (what the agent actually
    needs to read to understand the full definition).
    """
    t = node.type

    if t == "decorated_definition":
        # The first non-decorator child is the actual function or class.
        inner = next(
            (c for c in node.children if c.type in ("function_definition", "class_definition")),
            None,
        )
        if inner is None:
            return
        name_node = inner.child_by_field_name("name")
        if name_node is None:
            return
        name = name_node.text.decode()
        # Line span covers the decorator (node) not just the inner def
        start = node.start_point[0] + 1   # tree-sitter is 0-based
        end = node.end_point[0] + 1

        if inner.type == "class_definition":
            out.append(_sym(rel_path, name, SymbolKind.class_, start, end, None))
            # Recurse into class body with this class as the parent context
            body = inner.child_by_field_name("body")
            if body:
                _walk_body(body, rel_path, parent_class=name, out=out)
        else:
            kind = SymbolKind.method if parent_class else SymbolKind.function
            out.append(_sym(rel_path, name, kind, start, end, parent_class))
            # Functions can contain nested functions; recurse with same parent
            body = inner.child_by_field_name("body")
            if body:
                _walk_body(body, rel_path, parent_class=parent_class, out=out)
        return

    if t == "class_definition":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = name_node.text.decode()
        start = node.start_point[0] + 1
        end = node.end_point[0] + 1
        out.append(_sym(rel_path, name, SymbolKind.class_, start, end, None))
        body = node.child_by_field_name("body")
        if body:
            _walk_body(body, rel_path, parent_class=name, out=out)
        return

    if t == "function_definition":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = name_node.text.decode()
        start = node.start_point[0] + 1
        end = node.end_point[0] + 1
        kind = SymbolKind.method if parent_class else SymbolKind.function
        out.append(_sym(rel_path, name, kind, start, end, parent_class))
        body = node.child_by_field_name("body")
        if body:
            _walk_body(body, rel_path, parent_class=parent_class, out=out)
        return

    # For any other node type, recurse into children
    for child in node.children:
        _walk(child, rel_path, parent_class=parent_class, out=out)


def _walk_body(body_node, rel_path: str, parent_class: str | None, out: list[dict]) -> None:
    """Walk only the direct children of a class/function body."""
    for child in body_node.children:
        _walk(child, rel_path, parent_class=parent_class, out=out)


def _sym(
    file_path: str,
    name: str,
    kind: SymbolKind,
    start_line: int,
    end_line: int,
    parent_name: str | None,
) -> dict:
    return {
        "file_path": file_path,
        "name": name,
        "kind": kind,
        "start_line": start_line,
        "end_line": end_line,
        "parent_name": parent_name,
    }



# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def index_workspace(workspace_id: str, worktree_path: Path) -> dict:
    """
    Index (or incrementally re-index) all Python files in worktree_path.

    For each .py file:
      - If its SHA256 matches the stored file_index_state hash: skip.
      - Otherwise: delete old Symbol rows for that file, re-parse, insert
        fresh Symbol rows, update file_index_state.

    Returns a summary dict:
        {
          "files_indexed": int,   # actually re-parsed
          "files_skipped": int,   # hash unchanged
          "files_deleted": int,   # removed from workspace
          "symbols_added": int,
        }

    This is async so the orchestrator can await it directly. The heavy
    tree-sitter work runs in asyncio.to_thread so it doesn't block the
    event loop.
    """
    py_files = await asyncio.to_thread(list_python_files, worktree_path)

    stats = {"files_total": len(py_files), "files_indexed": 0, "files_skipped": 0, "files_deleted": 0, "symbols_added": 0,}

    async with AsyncSessionLocal() as db:
        # Load all known hashes for this workspace in one query
        result = await db.execute(
            select(FileIndexState.file_path, FileIndexState.content_hash).where(
                FileIndexState.workspace_id == workspace_id
            )
        )
        known_hashes: dict[str, str] = {row[0]: row[1] for row in result.fetchall()}

        current_files = set(py_files)
        known_files = set(known_hashes.keys())

        deleted_files = known_files - current_files

        for rel_path in deleted_files:
            await db.execute(
                delete(Symbol).where(
                    Symbol.workspace_id == workspace_id,
                    Symbol.file_path == rel_path,
                )
            )

            await db.execute(
                delete(FileIndexState).where(
                    FileIndexState.workspace_id == workspace_id,
                    FileIndexState.file_path == rel_path,
                )
            )
        stats["files_deleted"] = len(deleted_files)

        for rel_path in py_files:
            current_hash = await asyncio.to_thread(content_hash, worktree_path, rel_path)

            if known_hashes.get(rel_path) == current_hash:
                stats["files_skipped"] += 1
                continue

            # File is new or changed: delete stale symbols and re-parse.
            await db.execute(
                delete(Symbol).where(
                    Symbol.workspace_id == workspace_id,
                    Symbol.file_path == rel_path,
                )
            )

            source = read_bytes(worktree_path,rel_path,)
            raw_symbols = await asyncio.to_thread(_extract_symbols, source, rel_path)

            for s in raw_symbols:
                db.add(Symbol(workspace_id=workspace_id, **s))

            # Upsert the file_index_state row (insert or update hash)
            existing = await db.execute(
                select(FileIndexState).where(
                    FileIndexState.workspace_id == workspace_id,
                    FileIndexState.file_path == rel_path,
                )
            )
            fis = existing.scalar_one_or_none()
            if fis is None:
                db.add(FileIndexState(
                    workspace_id=workspace_id,
                    file_path=rel_path,
                    content_hash=current_hash,
                ))
            else:
                fis.content_hash = current_hash

            stats["files_indexed"] += 1
            stats["symbols_added"] += len(raw_symbols)

        await db.commit()

    return stats
