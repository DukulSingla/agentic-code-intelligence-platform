from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from app.api import tasks, workspaces
from app.config import settings
from app.models import AsyncSessionLocal, init_db

# --- Structured logging ----------------------------------------------------

logging.basicConfig(level=settings.log_level, format="%(message)s")
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(settings.log_level)),
)
log = structlog.get_logger()

# --- Minimal in-process metrics ---------------------------------------------
# Deliberately not a full Prometheus client to keep Phase 1 dependency-light.
# Counters are coarse and process-local; swap for prometheus_client + a
# /metrics scrape target before running more than one app replica.
_metrics = {"requests_total": 0, "requests_failed_total": 0, "tasks_created_total": 0}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    await init_db()
    log.info("startup.complete", environment=settings.environment)
    yield
    log.info("shutdown.complete")


app = FastAPI(title="Sarvam Agentic Code-Intelligence Platform", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def request_logging_and_tracing(request: Request, call_next):
    """
    Every request gets a trace id (echoed back in the response header so a
    caller can correlate their request with our logs) and a structured log
    line with method, path, status, and latency. This is the per-request
    trace; per-task traces are the journal (see app/agent/orchestrator.py).
    """
    trace_id = request.headers.get("x-trace-id", str(uuid.uuid4()))
    start = time.monotonic()
    _metrics["requests_total"] += 1
    try:
        response: Response = await call_next(request)
    except Exception:
        _metrics["requests_failed_total"] += 1
        log.exception("request.unhandled_error", trace_id=trace_id, path=request.url.path)
        raise
    duration_ms = round((time.monotonic() - start) * 1000, 2)
    if response.status_code >= 500:
        _metrics["requests_failed_total"] += 1
    log.info(
        "request.complete",
        trace_id=trace_id,
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=duration_ms,
    )
    response.headers["x-trace-id"] = trace_id
    return response


app.include_router(workspaces.router)
app.include_router(tasks.router)


@app.get("/health", tags=["ops"])
async def health():
    """Liveness: the process is up and serving requests."""
    return {"status": "ok", "service": settings.service_name}


@app.get("/ready", tags=["ops"])
async def ready():
    """Readiness: dependencies (DB) are reachable. Used by orchestrators/compose healthchecks."""
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception as e:
        return Response(content=f'{{"status": "not_ready", "error": "{e}"}}', status_code=503, media_type="application/json")


@app.get("/metrics", tags=["ops"], response_class=PlainTextResponse)
async def metrics():
    """Prometheus exposition format, hand-rolled (see note above on _metrics)."""
    lines = [f"sci_{k} {v}" for k, v in _metrics.items()]
    return "\n".join(lines) + "\n"
