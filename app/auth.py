"""
API-key authentication.

Implemented as a FastAPI dependency (`require_user`) rather than raw ASGI
middleware, because every protected route needs a DB session anyway to look
the key up — a dependency lets us inject both in one place and guarantees
the check runs before any route body executes. Every route under /v1/*
declares this dependency; there is no route that skips it.

Keys are stored hashed (bcrypt via passlib). The plaintext key is shown to
the user exactly once, at creation time (see scripts/create_user.py).
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, get_db

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_api_key(plaintext: str) -> str:
    return _pwd_ctx.hash(plaintext)


def verify_api_key(plaintext: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plaintext, hashed)


async def require_user(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header. Expected: Bearer <api-key>",
        )
    plaintext_key = authorization.split(" ", 1)[1].strip()
    if not plaintext_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Empty API key")

    # Keys aren't indexed by plaintext (we never store plaintext), so we
    # check against each stored hash. Fine at the user counts this service
    # targets; if this becomes a bottleneck, switch to a fast-lookup prefix
    # (store first 8 chars of the key in plaintext as a lookup index,
    # verify the rest against the hash) rather than a flat scan.
    result = await db.execute(select(User))
    for user in result.scalars().all():
        if verify_api_key(plaintext_key, user.api_key_hash):
            return user

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
