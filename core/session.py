"""Conversation sessions: identity, short history, entities in play, pending action, OTP.

Text and voice share one Session per logged-in customer, so a question asked by
voice can be followed up in text and the other way round.

Sessions are in memory by default (one gateway process). With REDIS_URL set,
they are saved to Redis after each turn so several gateway processes can serve
the same customer.
"""

from __future__ import annotations

import json
import secrets
import time
import uuid
from dataclasses import asdict, dataclass, field

OTP_VALID_S = 300
HISTORY_TURNS = 4  # user+assistant pairs kept verbatim


@dataclass
class PendingAction:
    action_id: str
    name: str
    pack: str
    title: str
    params: dict
    details: dict
    summary: str
    requires_otp: bool
    idempotency_key: str
    status: str = "awaiting_confirmation"  # awaiting_otp | executed | cancelled | failed
    created: float = field(default_factory=time.time)
    result: dict | None = None


@dataclass
class Session:
    customer_id: str
    customer_name: str = ""
    scopes: list[str] = field(default_factory=lambda: ["read", "write"])
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    history: list[dict] = field(default_factory=list)
    entities: dict = field(default_factory=dict)
    active_pack: str = "loans"
    profile: dict = field(default_factory=dict)
    accounts: dict = field(default_factory=dict)  # prefetched products, keyed by pack
    pending_action: PendingAction | None = None
    otp_code: str | None = None
    otp_issued_at: float | None = None
    otp_verified_at: float | None = None
    phone_last4: str = ""

    # --- history -------------------------------------------------------------------
    def add_turn(self, user: str, assistant: str) -> None:
        self.history += [
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ]
        self.history = self.history[-HISTORY_TURNS * 2 :]

    def remember(self, **entities) -> None:
        for k, v in entities.items():
            if v not in (None, ""):
                self.entities[k] = v

    # --- OTP (simulated: the code is shown in the demo UI instead of sent by SMS) ---
    def issue_otp(self) -> str:
        self.otp_code = f"{secrets.randbelow(1_000_000):06d}"
        self.otp_issued_at = time.time()
        return self.otp_code

    def verify_otp(self, code: str) -> bool:
        digits = "".join(ch for ch in str(code) if ch.isdigit())
        ok = (
            self.otp_code is not None
            and self.otp_issued_at is not None
            and time.time() - self.otp_issued_at < OTP_VALID_S
            and secrets.compare_digest(digits, self.otp_code)
        )
        if ok:
            self.otp_verified_at = time.time()
            self.otp_code = None  # one use only
        return ok

    def otp_fresh(self) -> bool:
        return self.otp_verified_at is not None and time.time() - self.otp_verified_at < OTP_VALID_S

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> Session:
        d = json.loads(raw)
        pa = d.pop("pending_action")
        s = cls(**d)
        s.pending_action = PendingAction(**pa) if pa else None
        return s


class SessionStore:
    def __init__(self, redis_url: str = ""):
        self._mem: dict[str, Session] = {}
        self._redis = None
        if redis_url:
            import redis.asyncio as redis

            self._redis = redis.from_url(redis_url)

    async def get(self, session_id: str) -> Session | None:
        if session_id in self._mem:
            return self._mem[session_id]
        if self._redis is not None:
            raw = await self._redis.get(f"session:{session_id}")
            if raw:
                s = Session.from_json(raw)
                self._mem[session_id] = s
                return s
        return None

    async def save(self, s: Session) -> None:
        self._mem[s.session_id] = s
        if self._redis is not None:
            await self._redis.set(f"session:{s.session_id}", s.to_json(), ex=8 * 3600)
