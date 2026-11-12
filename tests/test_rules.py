from pathlib import Path

import pytest

from core.rules import RuleError, RulesEngine, evaluate_condition

ROOT = Path(__file__).resolve().parents[1]
ENGINE = RulesEngine.from_file(ROOT / "packs" / "loans" / "rules.yaml")


def loan(**kw):
    base = {
        "rate_type": "floating",
        "borrower_type": "individual",
        "purpose": "personal",
        "months_since_disbursal": 5,
    }
    return {**base, **kw}


def test_floating_loan_inside_lock_in_still_has_no_charge():
    v = ENGINE.evaluate("prepayment_charge", {"loan": loan(months_since_disbursal=3)})
    assert v.values["charge_pct"] == 0
    assert v.clause == "LT-6.1"


def test_fixed_loan_inside_lock_in_is_charged_4_pct():
    v = ENGINE.evaluate(
        "prepayment_charge", {"loan": loan(rate_type="fixed", months_since_disbursal=6)}
    )
    assert v.values["charge_pct"] == 4
    assert v.rule_id == "prepay.fixed.lock_in"


def test_fixed_loan_after_lock_in_is_charged_2_pct():
    v = ENGINE.evaluate(
        "prepayment_charge", {"loan": loan(rate_type="fixed", months_since_disbursal=26)}
    )
    assert v.values["charge_pct"] == 2


def test_business_purpose_floating_falls_through():
    v = ENGINE.evaluate("prepayment_charge", {"loan": loan(purpose="business")})
    assert v.rule_id == "prepay.default"


@pytest.mark.parametrize(
    "code, charge", [("BANK_TECHNICAL", 0), ("INSUFFICIENT_FUNDS", 500), ("MANDATE_EXPIRED", 500)]
)
def test_bounce_charge_by_failure_cause(code, charge):
    v = ENGINE.evaluate("failed_payment_charge", {"payment": {"failure_code": code}})
    assert v.values["bounce_charge"] == charge


def test_verdict_records_rules_checked():
    v = ENGINE.evaluate(
        "prepayment_charge", {"loan": loan(rate_type="fixed", months_since_disbursal=26)}
    )
    assert v.trace[0].endswith("no match") and v.trace[-1].endswith("matched")


@pytest.mark.parametrize(
    "expr",
    ["__import__('os').system('echo hi')", "loan.__class__", "open('x')", "[x for x in loan]"],
)
def test_code_cannot_run_from_rules(expr):
    with pytest.raises(RuleError):
        evaluate_condition(expr, {"loan": loan()})


def test_unknown_topic_rejected():
    with pytest.raises(RuleError):
        ENGINE.evaluate("nope", {})
