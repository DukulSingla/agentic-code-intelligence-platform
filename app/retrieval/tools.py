"""
Retrieval tools: the only interface the agent uses to navigate a codebase.

Rules this module enforces:
  1. Every tool is responsible for charging its retrieval work.
     Some tools charge once per call.
     Large scan tools may charge incrementally as work is performed.
  2. No function returns a whole file when a span would do. read_span_tool
     requires explicit start/end lines; reading line 1 to sys.maxsize is
     intentionally not a shortcut the agent can take without paying for it.
  3. Every function returns a ToolResult so the caller always has both
     the data and the token cost for journaling.
  4. All file access goes through reader.py -- never open() directly here.

The six tools:
  list_dir          List directory contents (files + subdirs). Very cheap.
  read_span_tool    Read exact line range from a file. Cost = chars / 4.
  search_symbols    DB lookup by name pattern. Cheap (no file reads).
  definition        Symbol lookup + read_span of its exact lines.
  references        AST walk across all files to find call sites.
  structural_grep   Regex search inside function/class bodies only.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

import tree_sitter_python as tspython
from sqlalchemy import select
from tree_sitter import Language, Parser

from app.models import AsyncSessionLocal, Symbol, SymbolKind
from app.retrieval import reader
from app.retrieval.budget import RetrievalBudget, estimate_tokens
from app.retrieval.reader import ReaderError

_PY_LANGUAGE = Language(tspython.language())

# threading.local() gives each thread its own Parser instance.
# This is the right scope for asyncio.to_thread work: the default
# ThreadPoolExecutor reuses threads across calls, so thread-local
# avoids both construction overhead (vs creating a new Parser per call)
# and the race condition a single global Parser would have under
# concurrent tasks (tree-sitter Parser is not thread-safe).
_local = threading.local()


def _get_parser() -> Parser:
    """Return this thread's Parser, creating it on first use."""
    if not hasattr(_local, "parser"):
        _local.parser = Parser(_PY_LANGUAGE)
    return _local.parser


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """
    Wrapper returned by every tool.  The agent reads `.data`; the
    orchestrator reads `.tokens_charged` for journaling.
    """
    tool: str
    data: object          # str | list[dict] -- depends on the tool
    tokens_charged: int
    meta: dict | None = None   # optional extra info (e.g. file_path, line range)


# ---------------------------------------------------------------------------
# 1. list_dir
# ---------------------------------------------------------------------------

def list_dir(worktree_path: Path, rel_path: str, budget: RetrievalBudget) -> ToolResult:
    """
    List files and subdirectories at rel_path inside the worktree.
    Returns a list of dicts: [{name, type: 'file'|'dir', path}].

    Flat cost: 5 tokens per entry (directory listings are metadata-only,
    the cheapest retrieval operation).
    """

    root = worktree_path.resolve()
    abs_path = reader.resolve_directory(worktree_path,rel_path,)

    entries = []
    for child in sorted(abs_path.iterdir()):
        entries.append({
            "name": child.name,
            "type": "dir" if child.is_dir() else "file",
            "path": str(child.relative_to(root)),
        })

    cost = max(1, len(entries) * 5)
    budget.charge("list_dir", cost)
    return ToolResult(tool="list_dir", data=entries, tokens_charged=cost,
                      meta={"path": rel_path, "count": len(entries)})


# ---------------------------------------------------------------------------
# 2. read_span_tool
# ---------------------------------------------------------------------------

def read_span_tool(
    worktree_path: Path,
    rel_file_path: str,
    start_line: int,
    end_line: int,
    budget: RetrievalBudget,
) -> ToolResult:
    """
    Read [start_line, end_line] (1-based, inclusive) from a file.

    This is the agent's only door into file content. It is intentionally
    NOT a "read whole file" shortcut -- the agent must name a specific span.
    Cost = len(returned text) // 4, so reading more costs more.
    """
    text = reader.read_span(worktree_path, rel_file_path, start_line, end_line)
    cost = estimate_tokens(text)
    budget.charge("read_span", cost)
    return ToolResult(
        tool="read_span",
        data=text,
        tokens_charged=cost,
        meta={"file": rel_file_path, "start": start_line, "end": end_line},
    )


# ---------------------------------------------------------------------------
# 3. search_symbols
# ---------------------------------------------------------------------------

async def search_symbols(
    workspace_id: str,
    query: str,
    budget: RetrievalBudget,
    *,
    kind: str | None = None,
) -> ToolResult:
    """
    Fuzzy symbol name search against the `symbols` table (LIKE %query%).
    Returns a list of symbol dicts: {name, kind, file_path, start_line,
    end_line, parent_name}.

    Optional `kind` filter: 'function' | 'class' | 'method'.

    Cost: 10 tokens flat (DB-only, no file reads) + 5 per result row.
    This is deliberately cheap so the agent can afford to search broadly
    before committing to reading specific spans.
    """
    async with AsyncSessionLocal() as db:
        q = select(Symbol).where(
            Symbol.workspace_id == workspace_id,
            Symbol.name.like(f"%{query}%"),
        )
        if kind:
            try:
                q = q.where(Symbol.kind == SymbolKind(kind))
            except ValueError as e:
                raise ReaderError(f"invalid symbol kind: {kind}") from e  # unknown kind filter -- raise error rather than hard error
        result = await db.execute(q.order_by(Symbol.file_path, Symbol.start_line))
        rows = result.scalars().all()

    data = [
        {
            "name": s.name,
            "kind": s.kind.value,
            "file_path": s.file_path,
            "start_line": s.start_line,
            "end_line": s.end_line,
            "parent_name": s.parent_name,
        }
        for s in rows
    ]
    cost = 10 + 5 * len(data)
    budget.charge("search_symbols", cost)
    return ToolResult(
        tool="search_symbols",
        data=data,
        tokens_charged=cost,
        meta={"query": query, "count": len(data)},
    )


# ---------------------------------------------------------------------------
# 4. definition
# ---------------------------------------------------------------------------

async def definition(
    workspace_id: str,
    symbol_name: str,
    worktree_path: Path,
    budget: RetrievalBudget,
    *,
    parent_name: str | None = None,
) -> ToolResult:
    """
    Look up a symbol's definition: find it in the symbols table, then read
    its exact source span from the worktree. Returns the source text plus
    location metadata.

    Budget is charged ONCE here (for the read_span), not in reader.py.
    The DB lookup itself is free (it's metadata, no file bytes).

    parent_name: pass the class name to disambiguate a method from a
    top-level function with the same name.
    """
    async with AsyncSessionLocal() as db:
        q = select(Symbol).where(
            Symbol.workspace_id == workspace_id,
            Symbol.name == symbol_name,
        )
        if parent_name is not None:
            q = q.where(Symbol.parent_name == parent_name)
        result = await db.execute(q.limit(20))
        matches = result.scalars().all()

        if not matches:
            raise ReaderError(
                f"symbol not found: {symbol_name!r}"
            )

        if len(matches) > 1:
            return ToolResult(
                tool="definition",
                data=None,
                tokens_charged=0,
                meta={
                    "ambiguous": True,
                    "candidates": [
                        {
                            "name": s.name,
                            "kind": s.kind.value,
                            "file_path": s.file_path,
                            "parent_name": s.parent_name,
                        }
                        for s in matches
                    ],
                },
            )

        sym = matches[0]

    # reader.read_span does the I/O -- no budget awareness there
    text = reader.read_span(worktree_path, sym.file_path, sym.start_line, sym.end_line)
    cost = estimate_tokens(text)
    # tools.py charges once, here
    budget.charge("definition", cost)

    return ToolResult(
        tool="definition",
        data=text,
        tokens_charged=cost,
        meta={
            "name": sym.name,
            "kind": sym.kind.value,
            "file_path": sym.file_path,
            "start_line": sym.start_line,
            "end_line": sym.end_line,
            "parent_name": sym.parent_name,
        },
    )


# ---------------------------------------------------------------------------
# 5. references
# ---------------------------------------------------------------------------

def references(
    symbol_name: str,
    worktree_path: Path,
    budget: RetrievalBudget,
) -> ToolResult:
    """
    Find every call site and name reference to symbol_name across the
    worktree, using tree-sitter AST walking.

    Returns a list of {file_path, line, text} dicts.

    This is the most expensive tool: it reads every .py file in the
    worktree to search for references. Cost = total chars read // 4.
    The agent should call search_symbols or definition first to confirm
    the symbol exists before calling references.
    """
    from app.retrieval.reader import list_python_files

    py_files = list_python_files(worktree_path)
    hits: list[dict] = []
    tokens_charged = 0
    seen: set[tuple[str, int]] = set()

    for rel_path in py_files:
        source = reader.read_bytes(
            worktree_path,
            rel_path,
        )

        file_cost = max(1,len(source) // 4,)

        budget.charge("references",file_cost,)

        tokens_charged += file_cost


        tree = _get_parser().parse(source)

        lines = source.decode(
            "utf-8",
            errors="replace",
        ).splitlines()

        _find_references(
            tree.root_node,
            symbol_name,
            rel_path,
            lines,
            hits,
            seen
        )

    # cost = max(1, total_chars // 4)
    # budget.charge("references", cost)
    return ToolResult(
        tool="references",
        data=hits,
        tokens_charged=tokens_charged,
        meta={
            "symbol": symbol_name,
            "files_searched": len(py_files),
            "hits": len(hits),
        },
    )


def _find_references(node, symbol_name: str, rel_path: str, lines: list[str], out: list[dict], seen: set[tuple[str, int]],) -> None:
    """
    Walk the AST looking for `identifier` nodes whose text matches
    symbol_name. We record a hit for:
      - call expressions: f(...)  where f == symbol_name
      - plain identifier references (assignments, imports, etc.)

    We deliberately skip the definition node itself by checking that we're
    not inside a function_definition/class_definition name field -- that
    avoids including "def get_users" as a reference to get_users.
    """
    if node.type == "identifier" and node.text.decode() == symbol_name:
        # Skip if this identifier IS the definition name
        parent = node.parent
        if parent and parent.type in ("function_definition","class_definition","decorated_definition",):
            name_field = parent.child_by_field_name("name")
            if name_field and name_field.id == node.id:
                for child in node.children:
                    _find_references(child, symbol_name, rel_path, lines, out, seen)
                return

        line_no = node.start_point[0]
        line_text = lines[line_no] if line_no < len(lines) else ""

        key = (rel_path, line_no + 1)

        if key not in seen:
            seen.add(key)

            out.append(
                {
                    "file_path": rel_path,
                    "line": line_no + 1,
                    "text": line_text.rstrip(),
                }
            )

    for child in node.children:
        _find_references(child, symbol_name, rel_path, lines, out, seen)


# ---------------------------------------------------------------------------
# 6. structural_grep
# ---------------------------------------------------------------------------

def structural_grep(
    worktree_path: Path,
    pattern: str,
    budget: RetrievalBudget,
    *,
    file_glob: str = "*.py",
) -> ToolResult:
    """
    Regex search inside function and class bodies only -- skipping string
    literals, comments, and module-level boilerplate.

    This is more precise than a raw text grep: `structural_grep("password")`
    won't match a docstring explaining what password means; it only matches
    the pattern inside actual executable code bodies.

    Returns a list of {file_path, line, text} dicts.
    Cost = total chars of file content searched // 4 (same as references,
    since we read every matching file).
    """
    try:
        regex = re.compile(pattern)
    except re.error as e:
        raise ReaderError(f"invalid regex pattern {pattern!r}: {e}") from e

    hits: list[dict] = []
    tokens_charged = 0

    for abs_path in sorted(worktree_path.rglob(file_glob)):
        parts = abs_path.relative_to(worktree_path).parts

        if any(
            p.startswith(".") or p == "__pycache__"
            for p in parts
        ):
            continue

        rel_path = str(
            abs_path.relative_to(worktree_path)
        )

        source = reader.read_bytes(
            worktree_path,
            rel_path,
        )

        file_cost = max(1,len(source) // 4,)

        budget.charge("structural_grep",file_cost,)

        tokens_charged += file_cost

        tree = _get_parser().parse(source)

        lines = source.decode(
            "utf-8",
            errors="replace",
        ).splitlines()

        _grep_bodies(
            tree.root_node,
            regex,
            rel_path,
            lines,
            hits,
        )

    return ToolResult(
        tool="structural_grep",
        data=hits,
        tokens_charged=tokens_charged,
        meta={"pattern": pattern, "glob": file_glob, "hits": len(hits)},
    )


def _grep_bodies(node, regex: re.Pattern, rel_path: str, lines: list[str], out: list[dict]) -> None:
    """
    Recursively walk the AST. When we enter a function or class body, grep
    each line of that body for the pattern. Skip string nodes and comments.
    """
    if node.type in ("function_definition", "class_definition"):
        body = node.child_by_field_name("body")
        if body:
            _grep_node_lines(body, regex, rel_path, lines, out)
        return

    if node.type == "decorated_definition":
        inner = next(
            (c for c in node.children if c.type in ("function_definition", "class_definition")),
            None,
        )
        if inner:
            body = inner.child_by_field_name("body")
            if body:
                _grep_node_lines(body, regex, rel_path, lines, out)
        return

    for child in node.children:
        _grep_bodies(child, regex, rel_path, lines, out)


def _grep_node_lines(
    node, regex: re.Pattern, rel_path: str, lines: list[str], out: list[dict]
) -> None:
    """
    For each line in node's span that isn't a string literal or comment,
    check if the regex matches and record a hit.
    """
    start = node.start_point[0]
    end = node.end_point[0]

    # Collect line numbers that are covered by string/comment nodes so we
    # can skip them (the "structural" part of structural_grep).
    skip_lines: set[int] = set()
    for child in node.children:
        if child.type in ("string", "comment", "expression_statement"):
            # expression_statement wrapping a string is a docstring
            if child.type == "expression_statement":
                inner = next((c for c in child.children if c.type == "string"), None)
                if inner:
                    for ln in range(inner.start_point[0], inner.end_point[0] + 1):
                        skip_lines.add(ln)
            elif child.type in ("string", "comment"):
                for ln in range(child.start_point[0], child.end_point[0] + 1):
                    skip_lines.add(ln)

    for ln in range(start, min(end + 1, len(lines))):
        if ln in skip_lines:
            continue
        if regex.search(lines[ln]):
            out.append({"file_path": rel_path, "line": ln + 1, "text": lines[ln].rstrip()})
