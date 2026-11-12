"""Action framework: the only path for writes.

Every action goes through the same steps:
  1. propose   the planner asks for an action; the pack's prepare function
               validates it against backend data and fills in the details
  2. confirm   the customer confirms (button, typed or spoken reply)
  3. step-up   actions marked requires_otp need an OTP verified in the last 5 min
  4. execute   POST to Service Ops with an idempotency key, so a retry or a
               double tap creates one request, not two
  5. audit     every outcome, success or failure, goes to the audit log
"""

from __future__ import annotations

import hashlib
import json
import uuid
from string import Formatter

from core.calculators import inr
from core.connectors import ConnectorHub, NotFound, SystemUnavailable
from core.registry import ActionSpec, Pack, ToolContext, validate_args
from core.session import PendingAction, Session
from core.tracing import span


class ActionError(ValueError):
    pass


def _fill(template: str, values: dict) -> str:
    """Fill {name} placeholders; numbers named *amount* are shown in rupees."""
    out = {}
    for _, field_name, _, _ in Formatter().parse(template):
        if not field_name:
            continue
        v = values.get(field_name, "")
        if isinstance(v, int | float) and "amount" in field_name:
            v = inr(v)
        out[field_name] = v
    return template.format(**out)


def idempotency_key(session: Session, name: str, params: dict) -> str:
    raw = json.dumps([session.session_id, name, params], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class ActionFramework:
    def __init__(self, hub: ConnectorHub):
        self.hub = hub

    async def propose(
        self, session: Session, pack: Pack, spec: ActionSpec, args: dict, ctx: ToolContext
    ) -> PendingAction:
        problems = validate_args(spec.params, spec.required, args)
        if problems:
            raise ActionError("; ".join(problems))
        if "write" not in session.scopes:
            raise ActionError("this session is not allowed to make changes")
        details, display = dict(args), dict(args)
        if spec.prepare:
            with span(f"prepare {spec.name}", "action"):
                try:
                    prepared = await pack.functions[spec.prepare](ctx, **args)
                except NotFound as e:
                    raise ActionError(str(e)) from e
            details.update(prepared.get("details", {}))
            display.update(prepared.get("display", {}))
        action = PendingAction(
            action_id=uuid.uuid4().hex[:8],
            name=spec.name,
            pack=pack.name,
            title=spec.title,
            params=args,
            details=details,
            summary=_fill(spec.confirm, display),
            requires_otp=spec.requires_otp,
            idempotency_key=idempotency_key(session, spec.name, args),
        )
        session.pending_action = action
        return action

    def needs_otp(self, session: Session, action: PendingAction) -> bool:
        return action.requires_otp and not session.otp_fresh()

    async def execute(self, session: Session, action: PendingAction, request_type: str) -> dict:
        if self.needs_otp(session, action):
            raise ActionError("OTP verification required")
        body = {"customer_id": session.customer_id, "type": request_type, "details": action.details}
        outcome, result = "failed", {}
        try:
            with span(f"execute {action.name}", "action"):
                result = await self.hub["serviceops"].post(
                    "/requests", json=body, idempotency_key=action.idempotency_key
                )
            outcome = "duplicate_ignored" if result.get("duplicate") else "created"
            action.status, action.result = "executed", result
            self.hub.cache.clear_customer(session.customer_id)
            return result
        except (SystemUnavailable, ValueError) as e:
            action.status = "failed"
            result = {"error": str(e)}
            raise
        finally:
            await self._audit(session, action, outcome, result)
            session.pending_action = None if action.status == "executed" else session.pending_action

    async def _audit(self, session: Session, action: PendingAction, outcome: str, result: dict):
        entry = {
            "actor": f"assistant (session {session.session_id[:8]})",
            "customer_id": session.customer_id,
            "action": action.name,
            "params": {
                **action.params,
                "request_id": result.get("request_id"),
                "error": result.get("error"),
            },
            "outcome": outcome,
            "idempotency_key": action.idempotency_key,
        }
        try:
            await self.hub["serviceops"].post("/audit", json=entry)
        except Exception:  # noqa: BLE001 - audit failure must not hide the action result
            import logging

            logging.getLogger(__name__).exception("audit log write failed")
