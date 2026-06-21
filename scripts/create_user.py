"""
One-off CLI to create a user and print their plaintext API key.

Usage (inside the app container or with the same DATABASE_URL configured):
    python -m scripts.create_user --name alice

The plaintext key is shown exactly once, here. Only the bcrypt hash is
persisted (see app/auth.py).
"""
from __future__ import annotations

import argparse
import asyncio
import secrets

from app.auth import hash_api_key
from app.models import AsyncSessionLocal, User, init_db


async def main(name: str) -> None:
    await init_db()
    plaintext_key = f"sk-sci-{secrets.token_urlsafe(32)}"
    async with AsyncSessionLocal() as db:
        user = User(name=name, api_key_hash=hash_api_key(plaintext_key))
        db.add(user)
        await db.commit()
        print(f"user_id: {user.id}")
        print(f"api_key: {plaintext_key}")
        print("\nStore this key now — it is not recoverable from the database.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.name))
