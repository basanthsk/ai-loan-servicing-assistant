"""Shared plumbing for the mock enterprise systems.

Each system is its own FastAPI app on its own port, so calls between the
assistant and the systems cross a real network hop. Every app gets:

- simulated latency and failures from services/faults.yaml, changeable at run
  time through PUT /admin/faults (used by the demo's fault-injection switch)
- /health
- a read-only SQLite connection to data/bank.db (Service Ops also writes)
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
import sys
from datetime import date
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.settings import get_settings  # noqa: E402

FAULTS_FILE = ROOT / "services" / "faults.yaml"


class Faults(BaseModel):
    latency_ms: float = 100
    jitter_ms: float = 30
    failure_rate: float = 0.0
    down: bool = False


def today() -> date:
    return date.fromisoformat(get_settings().demo_today)


def db(write: bool = False) -> sqlite3.Connection:
    path = ROOT / get_settings().db_path
    if not path.exists():
        raise HTTPException(500, "Database not found; run `poetry run poe seed` first")
    uri = f"file:{path}?mode={'rw' if write else 'ro'}"
    con = sqlite3.connect(uri, uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def create_app(system: str, lifespan=None) -> FastAPI:
    config = yaml.safe_load(FAULTS_FILE.read_text()).get(system, {})
    app = FastAPI(title=f"Mock {system} system", lifespan=lifespan)
    app.state.faults = Faults(**config)
    app.state.rng = random.Random()

    @app.middleware("http")
    async def inject_faults(request: Request, call_next):
        path = request.url.path
        if path == "/health" or path.startswith("/admin"):
            return await call_next(request)
        f: Faults = app.state.faults
        delay = max(0.0, app.state.rng.gauss(f.latency_ms, f.jitter_ms)) / 1000
        await asyncio.sleep(delay)
        if f.down:
            return JSONResponse({"error": f"{system} is unavailable"}, status_code=503)
        if app.state.rng.random() < f.failure_rate:
            return JSONResponse({"error": f"{system} transient failure"}, status_code=503)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"system": system, "status": "ok"}

    @app.get("/admin/faults")
    def get_faults() -> Faults:
        return app.state.faults

    @app.put("/admin/faults")
    def set_faults(f: Faults) -> Faults:
        app.state.faults = f
        return f

    return app


def require_owner(
    con: sqlite3.Connection, table: str, key: str, value: str, customer_id: str
) -> dict:
    """Fetch one row and check it belongs to the customer; 404 either way, so ids can't be probed."""
    row = con.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,)).fetchone()
    if row is None or row["customer_id"] != customer_id:
        raise HTTPException(404, f"{key} {value} not found for this customer")
    return dict(row)
