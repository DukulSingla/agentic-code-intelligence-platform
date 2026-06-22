"""
Phase 1 integration test: exercises the real FastAPI app (in-process, via
httpx ASGITransport) against a throwaway SQLite db and throwaway git repos
on disk. This is the "retrieval -> task -> verified change" test required
by the assignment will grow into a full pipeline test once Phases 2-4 land;
for now it covers the API/auth/isolation/journal plumbing those phases sit on.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    # Point every on-disk path + the DB at a throwaway tmp_path BEFORE
    # importing app modules, so the process-wide `settings` singleton and
    # SQLAlchemy engine are constructed against the test location.
    monkeypatch.setenv("SCI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/sci.db")
    monkeypatch.setenv("SCI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCI_REPOS_DIR", str(tmp_path / "repos"))
    monkeypatch.setenv("SCI_WORKTREES_DIR", str(tmp_path / "worktrees"))
    monkeypatch.setenv("SCI_JOURNAL_DIR", str(tmp_path / "journals"))

    import importlib

    import app.config as config_mod
    importlib.reload(config_mod)
    import app.models as models_mod
    importlib.reload(models_mod)
    import app.auth as auth_mod
    importlib.reload(auth_mod)
    import app.retrieval.workspace as ws_mod
    importlib.reload(ws_mod)
    import app.api.workspaces as workspaces_mod
    importlib.reload(workspaces_mod)
    import app.api.tasks as tasks_mod
    importlib.reload(tasks_mod)
    import app.main as main_mod
    importlib.reload(main_mod)

    await models_mod.init_db()  # ASGITransport doesn't run lifespan startup; do it explicitly

    async with AsyncClient(transport=ASGITransport(app=main_mod.app), base_url="http://test") as ac:
        yield ac, models_mod, auth_mod


@pytest.fixture
def sample_repo(tmp_path):
    """A tiny git repo to register as a workspace source."""
    repo = tmp_path / "src_repo"
    repo.mkdir()
    (repo / "app.py").write_text("def get_users():\n    return []\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    return repo


async def _create_user(models_mod, auth_mod, name: str) -> tuple[str, str]:
    plaintext_key = f"sk-test-{name}-key"
    async with models_mod.AsyncSessionLocal() as db:
        user = models_mod.User(name=name, api_key_hash=auth_mod.hash_api_key(plaintext_key))
        db.add(user)
        await db.commit()
        return user.id, plaintext_key


@pytest.mark.asyncio
async def test_health(client):
    ac, _, _ = client
    resp = await ac.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_unauthenticated_request_rejected(client):
    ac, _, _ = client
    resp = await ac.get("/v1/workspaces")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_workspace_and_task_lifecycle(client, sample_repo):
    ac, models_mod, auth_mod = client
    _, alice_key = await _create_user(models_mod, auth_mod, "alice")
    headers = {"Authorization": f"Bearer {alice_key}"}

    ws_resp = await ac.post(
        "/v1/workspaces", json={"name": "demo", "source": str(sample_repo)}, headers=headers,
    )
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    task_resp = await ac.post(
        "/v1/tasks",
        json={"workspace_id": ws_id, "instruction": "do a thing", "mode": "dry_run"},
        headers=headers,
    )
    assert task_resp.status_code == 202
    task_id = task_resp.json()["task_id"]

    get_resp = await ac.get(f"/v1/tasks/{task_id}", headers=headers)
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["state"] == "QUEUED"
    assert body["mode"] == "dry_run"

    # The task's worktree must exist on disk and be a real checkout, not
    # an empty dir — i.e. isolation is structural, not just a DB row.
    worktree_path = Path(models_mod.settings.worktrees_dir) / task_id
    assert (worktree_path / "app.py").exists()


@pytest.mark.asyncio
async def test_cross_user_isolation(client, sample_repo):
    ac, models_mod, auth_mod = client
    _, alice_key = await _create_user(models_mod, auth_mod, "alice")
    _, bob_key = await _create_user(models_mod, auth_mod, "bob")

    ws_resp = await ac.post(
        "/v1/workspaces",
        json={"name": "alices-repo", "source": str(sample_repo)},
        headers={"Authorization": f"Bearer {alice_key}"},
    )
    ws_id = ws_resp.json()["id"]

    # Bob must not be able to read Alice's workspace, and the list endpoint
    # must not leak it either.
    bob_headers = {"Authorization": f"Bearer {bob_key}"}
    assert (await ac.get(f"/v1/workspaces/{ws_id}", headers=bob_headers)).status_code == 404
    assert (await ac.get("/v1/workspaces", headers=bob_headers)).json() == []


@pytest.mark.asyncio
async def test_idempotent_task_creation(client, sample_repo):
    ac, models_mod, auth_mod = client
    _, alice_key = await _create_user(models_mod, auth_mod, "alice")
    headers = {"Authorization": f"Bearer {alice_key}"}

    ws_resp = await ac.post("/v1/workspaces", json={"name": "demo", "source": str(sample_repo)}, headers=headers)
    ws_id = ws_resp.json()["id"]

    payload = {"workspace_id": ws_id, "instruction": "v1", "idempotency_key": "fixed-key"}
    r1 = await ac.post("/v1/tasks", json=payload, headers=headers)
    payload["instruction"] = "v2 (should be ignored)"
    r2 = await ac.post("/v1/tasks", json=payload, headers=headers)

    assert r1.json()["task_id"] == r2.json()["task_id"]


@pytest.mark.asyncio
async def test_journal_dump_is_ordered_and_owner_scoped(client, sample_repo):
    ac, models_mod, auth_mod = client
    _, alice_key = await _create_user(models_mod, auth_mod, "alice")
    _, bob_key = await _create_user(models_mod, auth_mod, "bob")
    alice_headers = {"Authorization": f"Bearer {alice_key}"}

    ws_resp = await ac.post("/v1/workspaces", json={"name": "demo", "source": str(sample_repo)}, headers=alice_headers)
    ws_id = ws_resp.json()["id"]
    task_resp = await ac.post(
        "/v1/tasks", json={"workspace_id": ws_id, "instruction": "do a thing"}, headers=alice_headers,
    )
    task_id = task_resp.json()["task_id"]

    journal_resp = await ac.get(f"/v1/tasks/{task_id}/journal", headers=alice_headers)
    assert journal_resp.status_code == 200
    events = journal_resp.json()
    assert len(events) >= 1
    # step_index must be strictly increasing — the journal is an ordered log,
    # not just a bag of rows.
    assert [e["step_index"] for e in events] == sorted(e["step_index"] for e in events)
    assert events[0]["event_type"] == "state_transition"

    # Bob can't read Alice's journal — same isolation contract as the
    # task/workspace lookups themselves.
    bob_headers = {"Authorization": f"Bearer {bob_key}"}
    assert (await ac.get(f"/v1/tasks/{task_id}/journal", headers=bob_headers)).status_code == 404


@pytest.mark.asyncio
async def test_cancel_queued_task_finalizes_immediately(client, sample_repo):
    ac, models_mod, auth_mod = client
    _, alice_key = await _create_user(models_mod, auth_mod, "alice")
    headers = {"Authorization": f"Bearer {alice_key}"}

    ws_resp = await ac.post("/v1/workspaces", json={"name": "demo", "source": str(sample_repo)}, headers=headers)
    ws_id = ws_resp.json()["id"]
    task_resp = await ac.post(
        "/v1/tasks", json={"workspace_id": ws_id, "instruction": "do a thing"}, headers=headers,
    )
    task_id = task_resp.json()["task_id"]

    cancel_resp = await ac.post(f"/v1/tasks/{task_id}/cancel", headers=headers)
    assert cancel_resp.status_code == 200
    body = cancel_resp.json()
    assert body["state"] == "CANCELLED"
    assert body["completed_at"] is not None

    # Cancelling an already-terminal task is rejected, not silently accepted.
    second_cancel = await ac.post(f"/v1/tasks/{task_id}/cancel", headers=headers)
    assert second_cancel.status_code == 409

    # The journal must record the cancellation as a real state transition,
    # not just mutate the row silently.
    journal = (await ac.get(f"/v1/tasks/{task_id}/journal", headers=headers)).json()
    transitions = [e for e in journal if e["event_type"] == "state_transition"]
    assert transitions[-1]["payload"]["to"] == "CANCELLED"
    assert transitions[-1]["payload"]["reason"] == "user_requested_cancel"


@pytest.mark.asyncio
async def test_cannot_cancel_another_users_task(client, sample_repo):
    ac, models_mod, auth_mod = client
    _, alice_key = await _create_user(models_mod, auth_mod, "alice")
    _, bob_key = await _create_user(models_mod, auth_mod, "bob")
    alice_headers = {"Authorization": f"Bearer {alice_key}"}

    ws_resp = await ac.post("/v1/workspaces", json={"name": "demo", "source": str(sample_repo)}, headers=alice_headers)
    ws_id = ws_resp.json()["id"]
    task_resp = await ac.post(
        "/v1/tasks", json={"workspace_id": ws_id, "instruction": "do a thing"}, headers=alice_headers,
    )
    task_id = task_resp.json()["task_id"]

    bob_headers = {"Authorization": f"Bearer {bob_key}"}
    assert (await ac.post(f"/v1/tasks/{task_id}/cancel", headers=bob_headers)).status_code == 404

    # And it must genuinely still be cancellable by its real owner afterward —
    # Bob's attempt must not have mutated the state.
    assert (await ac.post(f"/v1/tasks/{task_id}/cancel", headers=alice_headers)).status_code == 200


@pytest.mark.asyncio
async def test_task_checkpoint_roundtrip_and_unique_constraint(client, sample_repo):
    """
    No orchestrator writes checkpoints yet (that lands with the Phase 3
    loop), but the table itself — schema, JSON snapshot round-trip, and the
    (task_id, step_index) uniqueness that resume logic depends on — must be
    correct now, the same way budget_ledger was schema-verified before the
    budget tracker module existed.
    """
    ac, models_mod, auth_mod = client
    _, alice_key = await _create_user(models_mod, auth_mod, "alice")
    headers = {"Authorization": f"Bearer {alice_key}"}

    ws_resp = await ac.post("/v1/workspaces", json={"name": "demo", "source": str(sample_repo)}, headers=headers)
    ws_id = ws_resp.json()["id"]
    task_resp = await ac.post(
        "/v1/tasks", json={"workspace_id": ws_id, "instruction": "do a thing"}, headers=headers,
    )
    task_id = task_resp.json()["task_id"]

    from sqlalchemy import select

    async with models_mod.AsyncSessionLocal() as db:
        db.add(models_mod.TaskCheckpoint(
            task_id=task_id, step_index=3, state=models_mod.TaskState.EDITING,
            context_snapshot={"plan": ["a", "b"], "tokens_used": 900},
        ))
        await db.commit()

        result = await db.execute(
            select(models_mod.TaskCheckpoint).where(models_mod.TaskCheckpoint.task_id == task_id)
        )
        rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].step_index == 3
        assert rows[0].state == models_mod.TaskState.EDITING
        assert rows[0].context_snapshot == {"plan": ["a", "b"], "tokens_used": 900}

        # A second checkpoint at the same step_index for the same task must
        # be rejected — resume logic assumes at most one checkpoint per step.
        with pytest.raises(Exception):
            db.add(models_mod.TaskCheckpoint(
                task_id=task_id, step_index=3, state=models_mod.TaskState.VERIFYING,
                context_snapshot={},
            ))
            await db.commit()
