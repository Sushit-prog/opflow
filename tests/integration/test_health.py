"""M1: /health endpoint must prove DB connectivity with a real query.

TEST_TAXONOMY M1: test_health_endpoint - "/health returns 200 with DB
connectivity confirmed (not just app-up)". Uses FastAPI's TestClient (httpx)
against the real app object; the endpoint executes SELECT current_timestamp
against the live Postgres, so a 200 here proves real DB connectivity.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.mark.integration
def test_health_endpoint_reports_db_up() -> None:
    with TestClient(app) as client:
        resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "up"
    # db_time proves a real query executed (non-empty timestamp string)
    assert body["db_time"]