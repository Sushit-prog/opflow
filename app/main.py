"""FastAPI application entrypoint (M1: infra only).

Phase 0/1 scope: schema + scaffolding. This module exposes only a /health
endpoint that proves DB connectivity with a real query. No poller, worker,
tool, or agent code lives here yet.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionLocal

logger = logging.getLogger("opflow.health")

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