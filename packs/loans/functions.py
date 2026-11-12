"""Pack functions for loan servicing.

Each function combines backend data, the rules engine and the calculators into
one tool result, so the language model never has to do arithmetic or apply a
rule itself. Rupee amounts are returned twice: as numbers and as *_inr text,
which the model is told to quote as is.
"""

from __future__ import annotations

from datetime import date

from core.calculators import emi_after_rate_change, foreclosure_quote, inr, prepayment_impact
from core.connectors import NotFound
from core.registry import ToolContext

FUNCTIONS: dict = {}


def fn(f):
    FUNCTIONS[f.__name__] = f
    return f


def nice_date(iso: str | None) -> str:
    if not iso:
        return ""
    d = date.fromisoformat(iso[:10])
    return f"{d.day} {d.strftime('%B %Y')}"


async def _loans(ctx: ToolContext) -> list[dict]:
    return await ctx.get("loans", "/customers/{customer_id}/loans", cache_ttl_s=60)


async def _loan(ctx: ToolContext, loan_id: str) -> dict:
    return await ctx.get("loans", f"/loans/{loan_id}", cache_ttl_s=60)


@fn
async def context(ctx: ToolContext) -> dict:
    """Prefetched at login and given to the planner, so 'my loan' resolves without a call."""
    return {
        "loans": [
            {
                "loan_id": lo["loan_id"],
                "product": lo["product"],
                "rate_type": lo["rate_type"],
                "current_rate_pct": lo["current_rate_pct"],
                "current_emi_inr": inr(lo["current_emi"]),
                "next_due_date": lo["next_due_date"],
                "outstanding_inr": inr(lo["outstanding_principal"]),
            }
            for lo in await _loans(ctx)
        ]
    }


@fn
async def next_emi(ctx: ToolContext, loan_id: str | None = None) -> dict:
    loans = [
        lo
        for lo in await _loans(ctx)
        if lo["next_due_date"] and (not loan_id or lo["loan_id"] == loan_id)
    ]
    if not loans:
        raise NotFound(f"no active loan {loan_id or ''} with an upcoming EMI".strip())
    items = [
        {
            "loan_id": lo["loan_id"],
            "product": lo["product"],
            "emi_inr": inr(lo["current_emi"]),
            "due_date": lo["next_due_date"],
            "due_date_text": nice_date(lo["next_due_date"]),
        }
        for lo in loans
    ]
    if len(items) == 1:
        i = items[0]
        say = (
            f"Your next EMI on loan {i['loan_id']} is {i['emi_inr']}, due on {i['due_date_text']}."
        )
    else:
        parts = [f"{i['emi_inr']} on {i['loan_id']} due {i['due_date_text']}" for i in items]
        say = f"You have {len(items)} EMIs coming up: " + "; ".join(parts) + "."
    return {"say": say, "upcoming": items}


@fn
async def explain_emi_change(ctx: ToolContext, loan_id: str) -> dict:
    d = await _loan(ctx, loan_id)
    inst = d["recent_installments"]
    if len(inst) < 2:
        return {
            "loan_id": loan_id,
            "emi_changed": False,
            "note": "Not enough EMIs paid yet to compare.",
        }
    prev, cur = inst[-2], inst[-1]
    changed = round(cur["emi"]) != round(prev["emi"])
    result = {
        "loan_id": loan_id,
        "emi_changed": changed,
        "previous_emi": {
            "due_date": prev["due_date"],
            "amount_inr": inr(prev["emi"]),
            "rate_pct": prev["rate_pct"],
        },
        "current_emi": {
            "due_date": cur["due_date"],
            "amount_inr": inr(cur["emi"]),
            "rate_pct": cur["rate_pct"],
        },
        "difference_inr": inr(cur["emi"] - prev["emi"]),
    }
    if changed and d["rate_type"] == "floating":
        resets = [
            r
            for r in d["rate_history"]
            if prev["due_date"] <= r["effective_date"] <= cur["due_date"]
        ]
        reset = resets[-1] if resets else d["rate_history"][-1]
        before = [r for r in d["rate_history"] if r["effective_date"] < reset["effective_date"]]
        old = before[-1] if before else reset
        check = emi_after_rate_change(
            prev["closing"],
            prev["rate_pct"],
            cur["rate_pct"],
            d["tenure_months"] - prev["installment_no"],
        )
        verdict = ctx.evaluate("rate_reset", {"loan": d})
        result.update(
            reason=reset["reason"],
            reset_effective_date=reset["effective_date"],
            rate_change=f"{old['rate_pct']}% to {reset['rate_pct']}%",
            benchmark=d["benchmark"],
            benchmark_change=f"{old['benchmark_rate_pct']}% to {reset['benchmark_rate_pct']}%",
            spread_pct=d["spread_pct"],
            tenure_unchanged=True,
            recalculated_emi_check_inr=check.as_dict()["new_emi_inr"],
            rule=verdict,
            clause=await ctx.clause(verdict["clause"]),
        )
    return result


@fn
async def simulate_prepayment(ctx: ToolContext, loan_id: str, amount: float) -> dict:
    d = await _loan(ctx, loan_id)
    verdict = ctx.evaluate("prepayment_charge", {"loan": d, "amount": amount})
    impact = prepayment_impact(
        d["outstanding_principal"],
        d["current_rate_pct"],
        d["current_emi"],
        d["remaining_installments"],
        amount,
        verdict["charge_pct"],
    )
    return {
        "loan_id": loan_id,
        "assumes": "prepayment received today, before the next EMI",
        **impact.as_dict(),
        "option_reduce_tenure": f"keep EMI {impact.as_dict()['current_emi_inr']}, finish "
        f"{impact.months_saved} months earlier, save {impact.as_dict()['interest_saved_reduce_tenure_inr']} interest",
        "option_reduce_emi": f"keep tenure, EMI falls to {impact.as_dict()['new_emi_inr']}, save "
        f"{impact.as_dict()['interest_saved_reduce_emi_inr']} interest",
        "rule": verdict,
        "clauses": [await ctx.clause(verdict["clause"]), await ctx.clause("LT-6.5")],
    }


@fn
async def check_foreclosure(ctx: ToolContext, loan_id: str) -> dict:
    d = await _loan(ctx, loan_id)
    verdict = ctx.evaluate("prepayment_charge", {"loan": d})
    quote = foreclosure_quote(
        d["outstanding_principal"],
        d["current_rate_pct"],
        d["days_since_last_emi"],
        verdict["charge_pct"],
    )
    return {
        "loan_id": loan_id,
        "penalty_free": verdict["penalty_free"],
        "charge_pct": verdict["charge_pct"],
        "months_since_disbursal": d["months_since_disbursal"],
        "rate_type": d["rate_type"],
        "quote_as_of": d["as_of"],
        **quote.as_dict(),
        "rule": verdict,
        "clauses": [await ctx.clause(verdict["clause"]), await ctx.clause("FC-1.1")],
    }


@fn
async def explain_failed_payments(
    ctx: ToolContext, months: int = 6, loan_id: str | None = None
) -> dict:
    params = {"months": months}
    if loan_id:
        params["loan_id"] = loan_id
    data = await ctx.get("payments", "/customers/{customer_id}/payments", params, cache_ttl_s=30)
    payments = data["payments"]
    retries = {p["retry_of"]: p for p in payments if p.get("retry_of")}
    items, total = [], 0.0
    for p in sorted((p for p in payments if p["status"] == "failed"), key=lambda p: p["due_date"]):
        verdict = ctx.evaluate("failed_payment_charge", {"payment": p})
        fix = retries.get(p["payment_id"])
        total += verdict["bounce_charge"]
        items.append(
            {
                "payment_id": p["payment_id"],
                "loan_id": p["loan_id"],
                "due_date": p["due_date"],
                "amount_inr": inr(p["amount"]),
                "what_happened": p["failure_reason"],
                "resolved": f"paid on {fix['attempt_date']} by {fix['mode']}"
                if fix
                else "not yet paid",
                "days_late": (
                    date.fromisoformat(fix["attempt_date"]) - date.fromisoformat(p["due_date"])
                ).days
                if fix
                else None,
                "bounce_charge_inr": inr(verdict["bounce_charge"]),
                "customer_at_fault": verdict["customer_at_fault"],
                "charge_can_be_disputed": verdict["bounce_charge"] > 0
                and verdict["can_raise_dispute"],
            }
        )
    clauses = [await ctx.clause("PAY-2.1")]
    if any("mandate" in (i["what_happened"] or "").lower() for i in items):
        clauses.append(await ctx.clause("PAY-2.2"))
    return {
        "period": f"{data['since']} to {data['until']}",
        "failed_count": len(items),
        "failed_payments": items,
        "total_bounce_charges_inr": inr(total),
        "clauses": clauses,
    }


@fn
async def loan_options(ctx: ToolContext, loan_id: str) -> dict:
    d = await _loan(ctx, loan_id)
    verdict = ctx.evaluate("prepayment_charge", {"loan": d})
    quote = foreclosure_quote(
        d["outstanding_principal"],
        d["current_rate_pct"],
        d["days_since_last_emi"],
        verdict["charge_pct"],
    )
    return {
        "loan_id": loan_id,
        "position": {
            "outstanding_inr": inr(d["outstanding_principal"]),
            "current_emi_inr": inr(d["current_emi"]),
            "rate": f"{d['current_rate_pct']}% {d['rate_type']}",
            "remaining_installments": d["remaining_installments"],
            "next_due_date": d["next_due_date"],
        },
        "options": [
            {
                "option": "part-prepay",
                "charge_pct": verdict["charge_pct"],
                "note": "shortens tenure by default, or lowers the EMI if preferred; ask for an amount to simulate",
            },
            {
                "option": "foreclose",
                "charge_pct": verdict["charge_pct"],
                "total_payable_today_inr": quote.as_dict()["total_payable_inr"],
            },
            {"option": "continue as scheduled", "note": f"{d['remaining_installments']} EMIs left"},
            {"option": "repayment statement", "note": "can be sent to the registered email"},
        ],
        "rule": verdict,
        "clause": await ctx.clause(verdict["clause"]),
    }


# --- Action prepare functions: validate against backend data and fill details -----


@fn
async def prepare_dispute(ctx: ToolContext, payment_id: str, reason: str) -> dict:
    p = await ctx.get("payments", f"/payments/{payment_id}")
    return {
        "details": {
            "payment_id": payment_id,
            "loan_id": p["loan_id"],
            "due_date": p["due_date"],
            "amount": p["amount"],
            "status": p["status"],
            "failure_code": p.get("failure_code"),
            "charge": p.get("charge"),
            "customer_reason": reason,
        },
        "display": {"due_date": nice_date(p["due_date"]), "charge": inr(p.get("charge") or 0)},
    }


@fn
async def prepare_prepayment(
    ctx: ToolContext, loan_id: str, amount: float, mode: str = "reduce_tenure"
) -> dict:
    sim = await simulate_prepayment(ctx, loan_id, amount)
    mode_text = (
        "keep the EMI and shorten the tenure"
        if mode == "reduce_tenure"
        else "keep the tenure and lower the EMI"
    )
    return {
        "details": {
            "loan_id": loan_id,
            "amount": amount,
            "mode": mode,
            "charge": sim["charge"],
            "charge_pct": sim["charge_pct"],
        },
        "display": {"mode_text": mode_text, "charge_inr": sim["charge_inr"]},
    }


@fn
async def prepare_foreclosure(ctx: ToolContext, loan_id: str) -> dict:
    q = await check_foreclosure(ctx, loan_id)
    return {
        "details": {
            "loan_id": loan_id,
            "total_payable": q["total_payable"],
            "charge": q["charge"],
            "quote_as_of": q["quote_as_of"],
        },
        "display": {"total_payable_inr": q["total_payable_inr"]},
    }


@fn
async def prepare_statement(ctx: ToolContext, loan_id: str, period: str = "last 12 months") -> dict:
    await _loan(ctx, loan_id)  # validates ownership
    return {
        "details": {"loan_id": loan_id, "period": period, "delivery": "registered email"},
        "display": {},
    }
