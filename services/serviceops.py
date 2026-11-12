"""Mock Service Operations system: create requests, track status, escalate; audit log.

The only system that accepts writes. Requests are idempotent: a repeated POST
with the same Idempotency-Key returns the original request instead of a new one.
"""

import json
from datetime import datetime, timedelta

from fastapi import Header, HTTPException
from pydantic import BaseModel

from services.common import create_app, db, require_owner, rows, today

app = create_app("serviceops")

REQUEST_TYPES = {
    # type: (prefix, working days to resolve)
    "payment_dispute": ("DSP", 7),
    "prepayment_request": ("PPR", 2),
    "foreclosure_request": ("FCL", 7),
    "repayment_statement": ("STM", 0),
    "servicing_ticket": ("TKT", 3),
    "card_dispute": ("CDS", 7),
    "card_block": ("CBK", 0),
}


class NewRequest(BaseModel):
    customer_id: str
    type: str
    details: dict
    channel: str = "assistant"


class AuditEntry(BaseModel):
    actor: str
    customer_id: str
    action: str
    params: dict
    outcome: str
    idempotency_key: str | None = None


def add_working_days(start, days: int):
    d = start
    while days > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


def now_iso() -> str:
    # Demo date with the real time of day, so ordering within a session works.
    return f"{today().isoformat()}T{datetime.now().strftime('%H:%M:%S')}"


@app.post("/requests", status_code=201)
def create_request(req: NewRequest, idempotency_key: str = Header(...)):
    if req.type not in REQUEST_TYPES:
        raise HTTPException(422, f"unknown request type {req.type}")
    with db(write=True) as con:
        existing = con.execute(
            "SELECT * FROM service_requests WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if existing:
            r = dict(existing)
            r["details"] = json.loads(r["details"])
            r["duplicate"] = True
            return r
        prefix, days = REQUEST_TYPES[req.type]
        n = con.execute("SELECT COUNT(*) FROM service_requests").fetchone()[0] + 1
        row = dict(
            request_id=f"{prefix}-{today().strftime('%y%m%d')}-{n:04d}",
            customer_id=req.customer_id,
            type=req.type,
            status="completed" if days == 0 else "open",
            details=json.dumps(req.details),
            idempotency_key=idempotency_key,
            created_at=now_iso(),
            sla_due=add_working_days(today(), days).isoformat(),
            channel=req.channel,
        )
        con.execute(
            f"INSERT INTO service_requests ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            list(row.values()),
        )
        con.commit()
        return {**row, "details": req.details, "duplicate": False}


@app.get("/customers/{customer_id}/requests")
def customer_requests(customer_id: str, limit: int = 20):
    with db() as con:
        out = rows(
            con.execute(
                "SELECT * FROM service_requests WHERE customer_id=? ORDER BY created_at DESC LIMIT ?",
                (customer_id, limit),
            )
        )
    for r in out:
        r["details"] = json.loads(r["details"])
    return out


@app.get("/requests/{request_id}")
def get_request(request_id: str, customer_id: str):
    with db() as con:
        r = require_owner(con, "service_requests", "request_id", request_id, customer_id)
    r["details"] = json.loads(r["details"])
    return r


@app.post("/requests/{request_id}/escalate")
def escalate(request_id: str, customer_id: str):
    with db(write=True) as con:
        require_owner(con, "service_requests", "request_id", request_id, customer_id)
        con.execute(
            "UPDATE service_requests SET status='escalated' WHERE request_id=?", (request_id,)
        )
        con.commit()
    return get_request(request_id, customer_id)


@app.post("/audit", status_code=201)
def audit(entry: AuditEntry):
    with db(write=True) as con:
        con.execute(
            "INSERT INTO audit_log (ts, actor, customer_id, action, params, outcome, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                now_iso(),
                entry.actor,
                entry.customer_id,
                entry.action,
                json.dumps(entry.params),
                entry.outcome,
                entry.idempotency_key,
            ),
        )
        con.commit()
    return {"ok": True}


@app.get("/audit")
def audit_log(customer_id: str, limit: int = 20):
    with db() as con:
        out = rows(
            con.execute(
                "SELECT * FROM audit_log WHERE customer_id=? ORDER BY id DESC LIMIT ?",
                (customer_id, limit),
            )
        )
    for r in out:
        r["params"] = json.loads(r["params"])
    return out
