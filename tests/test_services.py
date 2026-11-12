"""Mock systems: data, ownership checks, idempotency and fault injection (in-process)."""

import pytest
from fastapi.testclient import TestClient

from services import crm, loans, payments, policy, serviceops
from services.common import Faults


@pytest.fixture(autouse=True)
def no_latency():
    """Run tests without simulated latency or random failures."""
    apps = [crm.app, loans.app, payments.app, policy.app, serviceops.app]
    saved = [a.state.faults for a in apps]
    for a in apps:
        a.state.faults = Faults(latency_ms=0, jitter_ms=0, failure_rate=0)
    yield
    for a, f in zip(apps, saved, strict=True):
        a.state.faults = f


def test_demo_loan_shows_rate_reset_and_higher_emi():
    c = TestClient(loans.app)
    d = c.get("/loans/L-1042", params={"customer_id": "C1001"}).json()
    assert d["rate_type"] == "floating"
    assert d["current_rate_pct"] == 10.75
    emis = [i["emi"] for i in d["recent_installments"]]
    assert emis[-1] > emis[-2]  # Nov EMI higher than Oct EMI
    assert d["rate_history"][-1]["effective_date"] == "2026-10-15"


def test_loan_of_another_customer_is_not_found():
    c = TestClient(loans.app)
    assert c.get("/loans/L-1042", params={"customer_id": "C1002"}).status_code == 404


def test_fixed_loan_is_inside_lock_in():
    c = TestClient(loans.app)
    lo = {x["loan_id"]: x for x in c.get("/customers/C1001/loans").json()}
    assert lo["L-1043"]["rate_type"] == "fixed"
    assert lo["L-1043"]["months_since_disbursal"] < 12


def test_three_failed_payments_in_six_months():
    c = TestClient(payments.app)
    r = c.get("/customers/C1001/payments", params={"status": "failed", "months": 6}).json()
    codes = sorted(p["failure_code"] for p in r["payments"])
    assert codes == ["BANK_TECHNICAL", "INSUFFICIENT_FUNDS", "MANDATE_EXPIRED"]


def test_policy_clause_lookup():
    c = TestClient(policy.app)
    assert "regardless of any lock-in" in c.get("/clauses/LT-6.1").json()["text"]


def test_service_request_is_idempotent(tmp_path, monkeypatch):
    c = TestClient(serviceops.app)
    body = {"customer_id": "C1001", "type": "servicing_ticket", "details": {"note": "test"}}
    key = "test-key-123"
    first = c.post("/requests", json=body, headers={"Idempotency-Key": key}).json()
    second = c.post("/requests", json=body, headers={"Idempotency-Key": key}).json()
    assert second["request_id"] == first["request_id"]
    assert second["duplicate"] is True


def test_fault_switch_makes_system_unavailable():
    c = TestClient(payments.app)
    c.put("/admin/faults", json={"latency_ms": 0, "jitter_ms": 0, "failure_rate": 0, "down": True})
    assert c.get("/customers/C1001/payments").status_code == 503
    assert c.get("/health").status_code == 200
