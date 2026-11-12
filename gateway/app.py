"""Gateway: login, OTP, chat WebSocket, account panel API, metrics, fault switches, voice.

Run with the mock systems: poetry run poe up  ->  http://127.0.0.1:8000

Auth: the demo login issues a JWT for a chosen demo customer (no password; the
identity provider is simulated). Every API call and the WebSocket need the
token, and the customer id used for backend calls always comes from it, never
from the browser or the model.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.assistant import Assistant, build_assistant
from core.connectors import SystemUnavailable
from core.session import Session
from core.tracing import METRICS
from gateway.runtime import Runtimes

log = logging.getLogger("gateway")
ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
TOKEN_TTL_S = 8 * 3600
SYSTEMS = ["crm", "loans", "payments", "policy", "serviceops", "cards"]

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    assistant = build_assistant()
    state["assistant"] = assistant
    secret = assistant.settings.jwt_secret
    if len(secret) < 32:
        # No strong secret configured: use a random one (logins end when the gateway restarts).
        log.warning("JWT_SECRET is short or unset; using a random secret for this run")
        secret = secrets.token_hex(32)
    state["jwt_secret"] = secret
    state["runtimes"] = Runtimes(assistant)
    state["admin"] = httpx.AsyncClient(timeout=3, trust_env=False)
    saver = asyncio.create_task(_save_metrics_every(15))
    yield
    saver.cancel()
    METRICS.save(LIVE_METRICS)
    await assistant.hub.aclose()
    await state["admin"].aclose()


# Timings of this run (text and voice), saved so they can be shared after a test session.
LIVE_METRICS = Path(
    os.environ.get("LIVE_METRICS")
    or Path(__file__).resolve().parents[1] / "evals" / "results" / "live-metrics.json"
)


async def _save_metrics_every(seconds: float) -> None:
    while True:
        await asyncio.sleep(seconds)
        if METRICS.samples:
            try:
                METRICS.save(LIVE_METRICS)
            except OSError as e:
                log.warning("could not save live metrics: %s", e)


app = FastAPI(title="Loan servicing assistant", lifespan=lifespan)


def A() -> Assistant:
    return state["assistant"]


# --- auth ---------------------------------------------------------------------------


def make_token(session: Session) -> str:
    now = int(time.time())
    claims = {
        "sub": session.customer_id,
        "sid": session.session_id,
        "scopes": session.scopes,
        "iat": now,
        "exp": now + TOKEN_TTL_S,
    }
    return jwt.encode(claims, state["jwt_secret"], algorithm="HS256")


async def session_from_token(token: str) -> Session:
    try:
        claims = jwt.decode(token, state["jwt_secret"], algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise HTTPException(401, "invalid or expired session; please log in again") from e
    session = await A().sessions.get(claims["sid"])
    if session is None or session.customer_id != claims["sub"]:
        raise HTTPException(401, "session not found; please log in again")
    return session


async def current_session(authorization: Annotated[str, Header()] = "") -> Session:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    return await session_from_token(authorization[7:])


CurrentSession = Annotated[Session, Depends(current_session)]


# --- pages --------------------------------------------------------------------------


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=WEB), name="static")


# --- login --------------------------------------------------------------------------


class LoginRequest(BaseModel):
    customer_id: str


@app.get("/api/customers")
async def demo_customers():
    """Customers offered on the demo login screen."""
    try:
        return await A().hub["crm"].get("/customers", customer_id="-", params={"limit": 3})
    except SystemUnavailable as e:
        raise HTTPException(503, str(e)) from e


@app.post("/api/login")
async def login(body: LoginRequest):
    session = Session(customer_id=body.customer_id)
    try:
        await A().orchestrator.prefetch(session)
    except SystemUnavailable as e:
        raise HTTPException(503, f"cannot log in right now: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(404, "customer not found") from e
    await A().sessions.save(session)
    return {
        "token": make_token(session),
        "customer_id": session.customer_id,
        "name": session.customer_name,
        "phone_last4": session.phone_last4,
    }


# --- account panel ------------------------------------------------------------------


@app.get("/api/account")
async def account(session: CurrentSession):
    hub, cid = A().hub, session.customer_id

    async def safe(coro, default):
        try:
            return await coro
        except SystemUnavailable as e:
            return {"unavailable": e.system, "reason": e.reason}
        except Exception:  # noqa: BLE001
            return default

    loans, payments, requests, cards = await asyncio.gather(
        safe(hub["loans"].get(f"/customers/{cid}/loans", customer_id=cid, cache_ttl_s=30), []),
        safe(
            hub["payments"].get(
                f"/customers/{cid}/payments",
                customer_id=cid,
                params={"months": 6, "limit": 12},
                cache_ttl_s=30,
            ),
            {},
        ),
        safe(
            hub["serviceops"].get(
                f"/customers/{cid}/requests", customer_id=cid, params={"limit": 5}
            ),
            [],
        ),
        safe(hub["cards"].get(f"/customers/{cid}/cards", customer_id=cid, cache_ttl_s=30), []),
    )
    return {
        "customer": {"id": cid, "name": session.customer_name, "profile": session.profile},
        "loans": loans,
        "payments": payments,
        "requests": requests,
        "cards": cards,
    }


@app.get("/api/audit")
async def audit(session: CurrentSession):
    return (
        await A()
        .hub["serviceops"]
        .get(
            "/audit",
            customer_id=session.customer_id,
            params={"customer_id": session.customer_id, "limit": 10},
        )
    )


# --- metrics and fault switches -----------------------------------------------------


@app.get("/api/metrics")
async def metrics(session: CurrentSession):
    return {"latency": METRICS.summary(), "circuit_breakers": A().hub.breaker_states()}


class FaultUpdate(BaseModel):
    latency_ms: float | None = None
    failure_rate: float | None = None
    down: bool | None = None


@app.get("/api/faults")
async def get_faults(session: CurrentSession):
    out = {}
    for name in SYSTEMS:
        try:
            r = await state["admin"].get(A().settings.system_url(name) + "/admin/faults")
            out[name] = r.json()
        except httpx.HTTPError:
            out[name] = {"unreachable": True}
    return out


@app.put("/api/faults/{system}")
async def set_fault(system: str, body: FaultUpdate, session: CurrentSession):
    if system not in SYSTEMS:
        raise HTTPException(404, "unknown system")
    base = A().settings.system_url(system) + "/admin/faults"
    current = (await state["admin"].get(base)).json()
    current.update({k: v for k, v in body.model_dump().items() if v is not None})
    r = await state["admin"].put(base, json=current)
    A().hub.cache.clear_customer(session.customer_id)
    return r.json()


# --- chat WebSocket -----------------------------------------------------------------


def confirm_fn(session: Session, approve: bool):
    return lambda emit: A().orchestrator.confirm_action(session, approve, emit)


def otp_fn(session: Session, code: str):
    return lambda emit: A().orchestrator.submit_otp(session, code, emit)


@app.websocket("/ws")
async def chat(ws: WebSocket, token: str = Query(...)):
    await ws.accept()
    try:
        session = await session_from_token(token)
    except HTTPException as e:
        await ws.send_json({"type": "error", "text": e.detail})
        await ws.close(code=4401)
        return
    runtime = state["runtimes"].get(session)
    runtime.listeners.add(ws)
    await ws.send_json({"type": "ready", "name": session.customer_name})
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind == "message" and msg.get("text", "").strip():
                runtime.start_turn(msg["text"].strip(), channel="text")
            elif kind == "confirm":
                runtime.start_action(confirm_fn(session, bool(msg.get("approve"))))
            elif kind == "otp":
                runtime.start_action(otp_fn(session, str(msg.get("code", ""))))
            elif kind == "cancel":
                await runtime.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        runtime.listeners.discard(ws)


# --- voice -------------------------------------------------------------------------
try:
    from gateway.voice import router as voice_router

    app.include_router(voice_router)
except ImportError as e:  # voice dependencies missing: text still works
    log.warning("voice disabled: %s", e)
