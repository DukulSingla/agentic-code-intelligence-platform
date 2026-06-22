"""
Phase 2 retrieval layer tests.

Covers: reader.py, indexer.py, budget.py, tools.py -- against the
eval/sample_repo fixture. These run entirely in-process, no HTTP layer.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio

SAMPLE_REPO = Path(__file__).parent.parent / "eval" / "sample_repo"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    """Throwaway SQLite db + fresh settings for every test."""
    monkeypatch.setenv("SCI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("SCI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCI_REPOS_DIR", str(tmp_path / "repos"))
    monkeypatch.setenv("SCI_WORKTREES_DIR", str(tmp_path / "worktrees"))
    monkeypatch.setenv("SCI_JOURNAL_DIR", str(tmp_path / "journals"))

    import importlib
    import app.config as cfg; importlib.reload(cfg)
    import app.models as m; importlib.reload(m)

    await m.init_db()
    return m


# ---------------------------------------------------------------------------
# reader.py
# ---------------------------------------------------------------------------

class TestReader:
    def test_read_span_exact(self):
        from app.retrieval.reader import read_span
        text = read_span(SAMPLE_REPO, "app.py", 9, 11)
        assert "@app.get" in text
        assert "def get_users" in text
        assert "return _users" in text

    def test_read_span_clamps_beyond_eof(self):
        from app.retrieval.reader import read_span
        # Asking for 9-9999 should return from line 9 to EOF, no error
        text = read_span(SAMPLE_REPO, "app.py", 9, 9999)
        assert len(text) > 0

    def test_read_span_rejects_traversal(self):
        from app.retrieval.reader import read_span, ReaderError
        with pytest.raises(ReaderError, match="traversal"):
            read_span(SAMPLE_REPO, "../../../etc/passwd", 1, 1)

    def test_read_span_rejects_sibling_dir_bypass(self, tmp_path):
        """
        The old str.startswith() check had a bypass:
          worktree  = /workspaces/repo
          evil path = /workspaces/repo_evil  <- starts with same string
        relative_to() checks actual path ancestry, so this must be rejected.
        """
        from app.retrieval.reader import read_span, ReaderError
        repo = tmp_path / "repo"
        repo_evil = tmp_path / "repo_evil"
        repo.mkdir()
        repo_evil.mkdir()
        (repo_evil / "secret.py").write_text("SECRET=42\n")
        (repo / "safe.py").write_text("x = 1\n")

        # Confirm the old check would have passed this (demonstrating the bug)
        evil_abs = (repo_evil / "secret.py").resolve()
        assert str(evil_abs).startswith(str(repo.resolve()))  # old bug was real

        # Confirm relative_to() correctly rejects it
        with pytest.raises(ReaderError, match="traversal"):
            read_span(repo, "../repo_evil/secret.py", 1, 1)

        # Legitimate read in the same setup must still work
        assert "x = 1" in read_span(repo, "safe.py", 1, 1)

    def test_read_span_rejects_bad_range(self):
        from app.retrieval.reader import read_span, ReaderError
        with pytest.raises(ReaderError):
            read_span(SAMPLE_REPO, "app.py", 10, 5)

    def test_read_span_rejects_start_beyond_eof(self):
        from app.retrieval.reader import read_span, ReaderError
        with pytest.raises(ReaderError, match="beyond end of file"):
            read_span(SAMPLE_REPO, "app.py", 9999, 10000)

    def test_content_hash_is_stable(self):
        from app.retrieval.reader import content_hash
        h1 = content_hash(SAMPLE_REPO, "app.py")
        h2 = content_hash(SAMPLE_REPO, "app.py")
        assert h1 == h2
        assert len(h1) == 64  # SHA256 hex

    def test_content_hash_differs_for_different_files(self):
        from app.retrieval.reader import content_hash
        h1 = content_hash(SAMPLE_REPO, "app.py")
        h2 = content_hash(SAMPLE_REPO, "test_app.py")
        assert h1 != h2

    def test_list_python_files(self):
        from app.retrieval.reader import list_python_files
        files = list_python_files(SAMPLE_REPO)
        assert "app.py" in files
        assert "test_app.py" in files
        # Must be sorted
        assert files == sorted(files)


# ---------------------------------------------------------------------------
# budget.py
# ---------------------------------------------------------------------------

class TestBudget:
    def test_charge_and_remaining(self):
        from app.retrieval.budget import RetrievalBudget
        b = RetrievalBudget(max_tokens=100)
        b.charge("read_span", 30)
        assert b.used == 30
        assert b.remaining == 70

    def test_budget_exhausted_raises(self):
        from app.retrieval.budget import RetrievalBudget, BudgetExhausted
        b = RetrievalBudget(max_tokens=10)
        with pytest.raises(BudgetExhausted) as exc_info:
            b.charge("references", 100)
        assert exc_info.value.tool == "references"
        # Balance must be unchanged after the raise
        assert b.used == 0
        assert b.remaining == 10

    def test_snapshot_restore_preserves_balance(self):
        from app.retrieval.budget import RetrievalBudget
        b = RetrievalBudget(max_tokens=500)
        b.charge("search_symbols", 50)
        b.charge("read_span", 100)
        snap = b.snapshot()

        b2 = RetrievalBudget.from_snapshot(snap)
        assert b2.used == 150
        assert b2.remaining == 350
        assert len(b2.ledger) == 2

    def test_estimate_tokens_minimum_one(self):
        from app.retrieval.budget import estimate_tokens
        assert estimate_tokens("") == 1
        assert estimate_tokens("x" * 400) == 100


# ---------------------------------------------------------------------------
# indexer.py
# ---------------------------------------------------------------------------

class TestIndexer:
    @pytest.mark.asyncio
    async def test_indexes_sample_repo_symbols(self, db):
        from app.retrieval.indexer import index_workspace
        from sqlalchemy import select
        stats = await index_workspace("ws_1", SAMPLE_REPO)

        assert stats["files_total"] == 2
        assert stats["files_indexed"] == 2
        assert stats["files_skipped"] == 0
        assert stats["symbols_added"] >= 5   # 2 in app.py + 3 in test_app.py

        async with db.AsyncSessionLocal() as session:
            result = await session.execute(
                select(db.Symbol).where(db.Symbol.workspace_id == "ws_1")
            )
            syms = {s.name: s for s in result.scalars().all()}

        assert "get_users" in syms
        assert "get_user" in syms
        assert syms["get_users"].file_path == "app.py"
        assert syms["get_users"].start_line == 9
        assert syms["get_users"].end_line == 11

    @pytest.mark.asyncio
    async def test_incremental_reindex_skips_unchanged(self, db):
        from app.retrieval.indexer import index_workspace
        await index_workspace("ws_1", SAMPLE_REPO)
        stats2 = await index_workspace("ws_1", SAMPLE_REPO)

        # Everything should be skipped -- hashes unchanged
        assert stats2["files_indexed"] == 0
        assert stats2["files_skipped"] == 2
        assert stats2["symbols_added"] == 0

    @pytest.mark.asyncio
    async def test_reindex_on_changed_file(self, db, tmp_path):
        """
        Write a modified version of app.py to a temp worktree, index it,
        then modify the file and re-index. Only the changed file should
        be re-indexed; the unchanged file should be skipped.
        """
        import shutil, importlib
        import app.retrieval.indexer as idx_mod; importlib.reload(idx_mod)

        wt = tmp_path / "wt"
        shutil.copytree(SAMPLE_REPO, wt)

        await idx_mod.index_workspace("ws_2", wt)

        app_py = wt / "app.py"
        original = app_py.read_text()
        app_py.write_text(original + "\n\ndef new_function():\n    return 42\n")

        stats = await idx_mod.index_workspace("ws_2", wt)

        assert stats["files_indexed"] == 1
        assert stats["files_skipped"] == 1

        from sqlalchemy import select
        async with db.AsyncSessionLocal() as session:
            result = await session.execute(
                select(db.Symbol).where(
                    db.Symbol.workspace_id == "ws_2",
                    db.Symbol.name == "new_function",
                )
            )
            assert result.scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# tools.py
# ---------------------------------------------------------------------------

class TestTools:
    @pytest.fixture(autouse=True)
    def budget(self):
        from app.retrieval.budget import RetrievalBudget
        self.b = RetrievalBudget(max_tokens=100_000)

    def test_list_dir(self):
        from app.retrieval import tools
        r = tools.list_dir(SAMPLE_REPO, ".", self.b)
        names = [e["name"] for e in r.data]
        assert "app.py" in names
        assert "test_app.py" in names
        assert r.tokens_charged > 0

    def test_list_dir_rejects_traversal(self):
        from app.retrieval import tools
        from app.retrieval.reader import ReaderError
        with pytest.raises(ReaderError):
            tools.list_dir(SAMPLE_REPO, "../../etc", self.b)

    def test_read_span_tool_charges_budget(self):
        from app.retrieval import tools
        before = self.b.used
        r = tools.read_span_tool(SAMPLE_REPO, "app.py", 9, 11, self.b)
        assert self.b.used > before
        assert r.tokens_charged == self.b.used - before
        assert "get_users" in r.data

    def test_read_span_tool_does_not_read_whole_file(self):
        """Requesting 3 lines should return far fewer chars than the whole file."""
        from app.retrieval import tools
        full = (SAMPLE_REPO / "app.py").read_text()
        r = tools.read_span_tool(SAMPLE_REPO, "app.py", 9, 11, self.b)
        assert len(r.data) < len(full)

    @pytest.mark.asyncio
    async def test_search_symbols(self, db):
        import importlib
        import app.retrieval.indexer as idx_mod; importlib.reload(idx_mod)
        import app.retrieval.tools as tools_mod; importlib.reload(tools_mod)

        await idx_mod.index_workspace("ws_1", SAMPLE_REPO)
        r = await tools_mod.search_symbols("ws_1", "get_user", self.b)
        names = [s["name"] for s in r.data]
        assert "get_users" in names
        assert "get_user" in names

    @pytest.mark.asyncio
    async def test_definition_returns_exact_span(self, db):
        import importlib
        import app.retrieval.indexer as idx_mod; importlib.reload(idx_mod)
        import app.retrieval.tools as tools_mod; importlib.reload(tools_mod)

        await idx_mod.index_workspace("ws_1", SAMPLE_REPO)
        r = await tools_mod.definition("ws_1", "get_user", SAMPLE_REPO, self.b)
        assert "def get_user" in r.data
        assert r.meta["start_line"] == 14
        assert r.meta["end_line"] == 19

    @pytest.mark.asyncio
    async def test_definition_missing_symbol_raises(self, db):
        import importlib
        import app.retrieval.indexer as idx_mod; importlib.reload(idx_mod)
        import app.retrieval.tools as tools_mod; importlib.reload(tools_mod)
        from app.retrieval.reader import ReaderError

        await idx_mod.index_workspace("ws_1", SAMPLE_REPO)
        with pytest.raises(ReaderError, match="symbol not found"):
            await tools_mod.definition("ws_1", "nonexistent_symbol", SAMPLE_REPO, self.b)

    def test_references_finds_call_sites(self):
        from app.retrieval import tools
        r = tools.references("get_users", SAMPLE_REPO, self.b)
        file_paths = [h["file_path"] for h in r.data]
        assert "test_app.py" in file_paths
        # Should find the import and the call site
        assert len(r.data) >= 1

    def test_references_budget_enforced(self):
        from app.retrieval import tools
        from app.retrieval.budget import RetrievalBudget, BudgetExhausted
        tiny = RetrievalBudget(max_tokens=1)
        with pytest.raises(BudgetExhausted):
            tools.references("get_users", SAMPLE_REPO, tiny)

    def test_structural_grep_finds_return_statements(self):
        from app.retrieval import tools
        r = tools.structural_grep(SAMPLE_REPO, r"return", self.b)
        assert len(r.data) >= 2
        for hit in r.data:
            assert "return" in hit["text"]

    def test_structural_grep_skips_comments_and_docstrings(self, tmp_path):
        """Pattern inside a docstring must NOT be returned."""
        import shutil
        from app.retrieval import tools
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "foo.py").write_text(
            '"""This docstring mentions SECRET but should not match."""\n'
            "def foo():\n"
            "    x = 1  # SECRET in comment should also not match\n"
            "    return x\n"
        )
        r = tools.structural_grep(wt, r"SECRET", self.b)
        # The grep should return 0 hits because SECRET only appears in
        # a module-level docstring and a comment, not in an executable body.
        assert len(r.data) == 0

    def test_budget_not_charged_by_reader(self, db, tmp_path):
        """
        reader.py must never charge the budget. Confirm that the budget
        used after a definition() call equals exactly estimate_tokens(result).
        If reader.py were also charging, we'd see double the expected amount.
        """
        import asyncio, importlib
        import app.retrieval.indexer as idx_mod; importlib.reload(idx_mod)
        import app.retrieval.tools as tools_mod; importlib.reload(tools_mod)
        from app.retrieval.budget import RetrievalBudget, estimate_tokens

        async def run():
            await idx_mod.index_workspace("ws_check", SAMPLE_REPO)
            b = RetrievalBudget(max_tokens=100_000)
            result = await tools_mod.definition("ws_check", "get_users", SAMPLE_REPO, b)
            expected = estimate_tokens(result.data)
            assert b.used == expected, (
                f"double-charging detected: used {b.used} but expected {expected}"
            )

        asyncio.run(run())
