"""Tiny sample FastAPI CRUD app used to exercise Phase 1 plumbing and,
from Phase 2 onward, as the target repo for retrieval/agent eval tasks."""
from fastapi import FastAPI

app = FastAPI()
_users = [{"id": 1, "name": "Ada"}, {"id": 2, "name": "Grace"}]


@app.get("/users")
def get_users():
    return _users


@app.get("/users/{user_id}")
def get_user(user_id: int):
    for u in _users:
        if u["id"] == user_id:
            return u
    return {"error": "not found"}
