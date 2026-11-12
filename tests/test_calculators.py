"""Calculator tests against independently checked figures.

Reference EMIs were computed with the standard formula P*r*(1+r)^n / ((1+r)^n - 1)
and cross-checked against bank EMI calculators (rounded to the rupee).
"""

import pytest

from core.calculators import (
    amortisation,
    emi,
    emi_after_rate_change,
    foreclosure_quote,
    inr,
    months_to_repay,
    prepayment_impact,
    total_interest,
)


@pytest.mark.parametrize(
    "principal, rate, months, expected",
    [
        (500_000, 10.75, 60, 10_809),  # personal loan
        (1_000_000, 12.0, 36, 33_214),
        (2_500_000, 8.5, 240, 21_696),  # home loan
    ],
)
def test_emi_matches_reference(principal, rate, months, expected):
    assert round(emi(principal, rate, months)) == expected


def test_zero_rate_emi_is_simple_division():
    assert emi(120_000, 0, 12) == 10_000


def test_amortisation_ends_at_zero_and_sums_to_principal():
    rows = amortisation(500_000, 10.75, 60)
    assert len(rows) == 60
    assert rows[-1].closing == pytest.approx(0, abs=0.01)
    assert sum(r.principal for r in rows) == pytest.approx(500_000, abs=0.01)


def test_months_to_repay_inverts_emi():
    payment = emi(500_000, 10.75, 60)
    assert months_to_repay(500_000, 10.75, payment) == 60


def test_months_to_repay_rejects_payment_below_interest():
    with pytest.raises(ValueError):
        months_to_repay(500_000, 12, 4_000)  # interest alone is 5,000 a month


def test_rate_rise_increases_emi():
    change = emi_after_rate_change(400_000, 10.25, 10.75, 44)
    assert change.new_emi > change.old_emi
    assert round(change.old_emi) == round(emi(400_000, 10.25, 44))
    assert change.difference == pytest.approx(change.new_emi - change.old_emi)


def test_prepayment_reduce_tenure_saves_interest_and_months():
    current = emi(400_000, 10.75, 44)
    impact = prepayment_impact(400_000, 10.75, current, 44, 50_000)
    assert impact.outstanding_after == 350_000
    assert impact.months_saved > 0
    assert impact.interest_saved_reduce_tenure > 0
    # Keeping the EMI and cutting tenure always saves more interest than cutting the EMI.
    assert impact.interest_saved_reduce_tenure > impact.interest_saved_reduce_emi > 0
    assert impact.new_emi == pytest.approx(emi(350_000, 10.75, 44))


def test_prepayment_charge():
    current = emi(400_000, 12, 30)
    impact = prepayment_impact(400_000, 12, current, 30, 50_000, charge_pct=4)
    assert impact.charge == 2_000


def test_total_interest_consistent_with_schedule():
    payment = emi(300_000, 11, 24)
    rows = amortisation(300_000, 11, 24)
    assert total_interest(300_000, 11, payment) == pytest.approx(
        sum(r.interest for r in rows), abs=0.5
    )


def test_foreclosure_quote():
    q = foreclosure_quote(200_000, 12.0, 15, 4.0)
    assert q.accrued_interest == pytest.approx(200_000 * 0.12 * 15 / 365)
    assert q.charge == 8_000
    assert q.total_payable == pytest.approx(200_000 + q.accrued_interest + 8_000)


@pytest.mark.parametrize(
    "amount, text",
    [(0, "₹0"), (999, "₹999"), (12450, "₹12,450"), (1234567.8, "₹12,34,568"), (-5000, "-₹5,000")],
)
def test_inr_formatting(amount, text):
    assert inr(amount) == text
