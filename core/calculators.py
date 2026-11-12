"""Loan calculators: pure functions, no I/O.

Every number the assistant states comes from these functions (or from backend
data), never from the language model. Money is rounded to the rupee at the
edges; internal arithmetic keeps full precision.

Conventions:
- annual_rate_pct is a percentage, e.g. 10.75 means 10.75% a year.
- Interest is monthly: annual rate / 12, on the reducing balance.
- EMIs are paid in arrears (end of each month).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass


def inr(amount: float) -> str:
    """Format rupees the Indian way: 1234567.8 -> '₹12,34,568'."""
    n = round(amount)
    sign = "-" if n < 0 else ""
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return f"{sign}₹{s}"


def monthly_rate(annual_rate_pct: float) -> float:
    return annual_rate_pct / 100 / 12


def emi(principal: float, annual_rate_pct: float, months: int) -> float:
    """Equated monthly instalment for a reducing-balance loan."""
    if months <= 0:
        raise ValueError("months must be positive")
    if principal <= 0:
        return 0.0
    r = monthly_rate(annual_rate_pct)
    if r == 0:
        return principal / months
    f = (1 + r) ** months
    return principal * r * f / (f - 1)


def months_to_repay(principal: float, annual_rate_pct: float, payment: float) -> int:
    """Number of monthly payments needed to repay principal with a fixed payment."""
    if principal <= 0:
        return 0
    r = monthly_rate(annual_rate_pct)
    if r == 0:
        return math.ceil(principal / payment)
    if payment <= principal * r:
        raise ValueError("payment does not cover the monthly interest; the loan never ends")
    n = -math.log(1 - principal * r / payment) / math.log(1 + r)
    return math.ceil(n - 1e-9)


@dataclass
class ScheduleRow:
    month: int
    opening: float
    emi: float
    interest: float
    principal: float
    closing: float


def amortisation(principal: float, annual_rate_pct: float, months: int) -> list[ScheduleRow]:
    """Month-by-month schedule; the last payment is trimmed so the balance ends at zero."""
    payment = emi(principal, annual_rate_pct, months)
    r = monthly_rate(annual_rate_pct)
    rows, balance = [], principal
    for m in range(1, months + 1):
        interest = balance * r
        pay = min(payment, balance + interest)
        principal_part = pay - interest
        closing = balance - principal_part
        rows.append(ScheduleRow(m, balance, pay, interest, principal_part, max(closing, 0.0)))
        balance = closing
        if balance <= 0.005:
            break
    return rows


def total_interest(principal: float, annual_rate_pct: float, payment: float) -> float:
    """Total interest paid when repaying principal with a fixed monthly payment."""
    r = monthly_rate(annual_rate_pct)
    balance, paid = principal, 0.0
    for _ in range(1200):  # 100-year guard
        if balance <= 0.005:
            break
        interest = balance * r
        pay = min(payment, balance + interest)
        paid += interest
        balance -= pay - interest
    return paid


@dataclass
class EmiChange:
    old_rate_pct: float
    new_rate_pct: float
    old_emi: float
    new_emi: float
    difference: float
    outstanding_at_reset: float
    remaining_months: int

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(
            old_emi_inr=inr(self.old_emi),
            new_emi_inr=inr(self.new_emi),
            difference_inr=inr(self.difference),
        )
        return d


def emi_after_rate_change(
    outstanding: float, old_rate_pct: float, new_rate_pct: float, remaining_months: int
) -> EmiChange:
    """EMI before and after a floating-rate reset, keeping the remaining tenure."""
    old = emi(outstanding, old_rate_pct, remaining_months)
    new = emi(outstanding, new_rate_pct, remaining_months)
    return EmiChange(old_rate_pct, new_rate_pct, old, new, new - old, outstanding, remaining_months)


@dataclass
class PrepaymentImpact:
    prepayment: float
    charge_pct: float
    charge: float
    outstanding_before: float
    outstanding_after: float
    current_emi: float
    remaining_months: int
    # Option A: keep the EMI, shorten the tenure
    new_remaining_months: int
    months_saved: int
    interest_saved_reduce_tenure: float
    # Option B: keep the tenure, lower the EMI
    new_emi: float
    emi_reduction: float
    interest_saved_reduce_emi: float

    def as_dict(self) -> dict:
        d = asdict(self)
        for k in (
            "prepayment",
            "charge",
            "outstanding_before",
            "outstanding_after",
            "current_emi",
            "interest_saved_reduce_tenure",
            "new_emi",
            "emi_reduction",
            "interest_saved_reduce_emi",
        ):
            d[f"{k}_inr"] = inr(d[k])
        return d


def prepayment_impact(
    outstanding: float,
    annual_rate_pct: float,
    current_emi: float,
    remaining_months: int,
    amount: float,
    charge_pct: float = 0.0,
) -> PrepaymentImpact:
    """Effect of a part-prepayment made today, before the next EMI."""
    if amount <= 0:
        raise ValueError("prepayment amount must be positive")
    amount = min(amount, outstanding)
    after = outstanding - amount
    base_interest = total_interest(outstanding, annual_rate_pct, current_emi)

    new_months = months_to_repay(after, annual_rate_pct, current_emi) if after > 0 else 0
    interest_a = total_interest(after, annual_rate_pct, current_emi) if after > 0 else 0.0

    new_emi = emi(after, annual_rate_pct, remaining_months) if after > 0 else 0.0
    interest_b = new_emi * remaining_months - after if after > 0 else 0.0
    base_b = current_emi * remaining_months - outstanding

    return PrepaymentImpact(
        prepayment=amount,
        charge_pct=charge_pct,
        charge=amount * charge_pct / 100,
        outstanding_before=outstanding,
        outstanding_after=after,
        current_emi=current_emi,
        remaining_months=remaining_months,
        new_remaining_months=new_months,
        months_saved=max(remaining_months - new_months, 0),
        interest_saved_reduce_tenure=base_interest - interest_a,
        new_emi=new_emi,
        emi_reduction=current_emi - new_emi,
        interest_saved_reduce_emi=base_b - interest_b,
    )


@dataclass
class ForeclosureQuote:
    outstanding: float
    accrued_interest: float
    charge_pct: float
    charge: float
    total_payable: float

    def as_dict(self) -> dict:
        d = asdict(self)
        for k in ("outstanding", "accrued_interest", "charge", "total_payable"):
            d[f"{k}_inr"] = inr(d[k])
        return d


def foreclosure_quote(
    outstanding: float, annual_rate_pct: float, days_since_last_emi: int, charge_pct: float
) -> ForeclosureQuote:
    """Amount to close the loan today: principal, interest accrued since the last EMI, charge."""
    accrued = outstanding * annual_rate_pct / 100 * days_since_last_emi / 365
    charge = outstanding * charge_pct / 100
    return ForeclosureQuote(
        outstanding, accrued, charge_pct, charge, outstanding + accrued + charge
    )
