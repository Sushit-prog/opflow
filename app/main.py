"""FastAPI application entrypoint.

M1: /health proves DB connectivity with a real query. M7 (follow-up):
POST /worker/round triggers one poller + worker round on demand, so the
whole automation pipeline can be exercised without running the CLI loop.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionLocal
from app.poller import poll_low_stock
from app.worker import run_worker_round

logger = logging.getLogger("opflow.api")

app = FastAPI(title="OpFlow", version="0.1.0")


def _db_ping() -> str:
    """Run a real query against the DB and return the server timestamp."""
    with SessionLocal() as session:
        value = session.execute(text("SELECT current_timestamp")).scalar_one()
        return str(value)


@app.get("/health", response_model=None)
def health() -> JSONResponse:
    """Liveness + DB connectivity check.

    Returns 200 only when the database answers a real query; 503 if the DB
    is unreachable (no raised exception escapes the handler).
    """
    try:
        db_time = _db_ping()
    except SQLAlchemyError as exc:  # includes OperationalError (connection refused)
        logger.error("health check failed: DB unreachable", exc_info=exc)
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "database": "down"},
        )

    return JSONResponse(
        status_code=200,
        content={"status": "ok", "database": "up", "db_time": db_time},
    )


@app.post("/worker/round", response_model=None)
def worker_round() -> JSONResponse:
    """On-demand trigger: one poller pass + one full worker round.

    Runs the same work as the CLI loop (app/runner.py) a single time:
    poll_low_stock enqueues date-scoped jobs for below-threshold items
    (idempotent), then run_worker_round claims and executes each due job
    through the capability-gated agent and completes it. Real work on every
    call - no mocks.
    """
    try:
        with SessionLocal() as session:
            touched = poll_low_stock(session)
            processed = run_worker_round(session)
    except Exception as exc:  # noqa: BLE001 - surface a clean JSON error
        logger.error("worker round failed", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(exc)},
        )

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "jobs_polled": [job.id for job in touched],
            "jobs_processed": processed,
        },
    )