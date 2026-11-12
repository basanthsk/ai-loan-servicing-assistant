"""Pack functions for credit-card servicing.

This pack reuses the platform unchanged (orchestrator, Decider, connectors, rules
engine, action framework, voice, UI). What it adds lives here: two card
calculators and the functions that combine card data, rules and calculators.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from core.calculators import inr
from core.registry import ToolContext

FUNCTIONS: dict = {}
GST_PCT = 18.0


def fn(f):
    FUNCTIONS[f.__name__] = f
    return f


def nice_date(iso: str | None) -> str:
    if not iso:
        return ""
    d = date.fromisoformat(iso[:10])
    return f"{d.day} {d.strftime('%B %Y')}"


# --- Card calculators (pack-local) ------------------------------------------------


def minimum_due(balance: float, pct: float = 5.0, floor: float = 200.0) -> float:
    """5% of the balance, at least Rs 200, never more than the balance."""
    if balance <= 0:
        return 0.0
    return min(balance, max(round(balance * pct / 100), floor))


def revolving_interest(
    statement_balance: float, paid: float, monthly_rate_pct: float, days: int = 30
) -> dict:
    """Interest for one cycle on the unpaid balance, plus GST on the interest.

    Simplified: the unpaid balance is carried for `days` at the monthly rate. Real
    statements also charge interest on new purchases from their date once the
    customer is revolving; this is stated as an assumption in the result.
    """
    carried = max(statement_balance - paid, 0.0)
    interest = carried * monthly_rate_pct / 100 * days / 30
    gst = interest * GST_PCT / 100
    return {
        "carried_forward": carried,
        "interest": interest,
        "gst_on_interest": gst,
        "total_cost": interest + gst,
    }


# --- Helpers ----------------------------------------------------------------------


async def _cards(ctx: ToolContext) -> list[dict]:
    return await ctx.get("cards", "/customers/{customer_id}/cards", cache_ttl_s=60)


async def _card(ctx: ToolContext, card_id: str | None) -> dict:
    cards = await _cards(ctx)
    if card_id:
        for c in cards:
            if card_id in (c["card_id"], c["last4"]):
                return c
    if len(cards) == 1 or (cards and not card_id):
        return cards[0]
    from core.connectors import NotFound

    raise NotFound(f"card {card_id} not found for this customer")


@fn
async def context(ctx: ToolContext) -> dict:
    return {
        "cards": [
            {
                "card_id": c["card_id"],
                "product": c["product"],
                "last4": c["last4"],
                "status": c["status"],
                "statement_balance_inr": inr(c.get("statement_balance", 0)),
                "minimum_due_inr": inr(c.get("minimum_due", 0)),
                "due_date": c.get("due_date"),
            }
            for c in await _cards(ctx)
        ]
    }


@fn
async def card_minimum_due(ctx: ToolContext, card_id: str | None = None) -> dict:
    c = await _card(ctx, card_id)
    say = (
        f"Your card ending {c['last4']} has a statement balance of {inr(c['statement_balance'])}. "
        f"The minimum due is {inr(c['minimum_due'])}, by {nice_date(c['due_date'])}."
    )
    return {
        "say": say,
        "card_id": c["card_id"],
        "statement_balance_inr": inr(c["statement_balance"]),
        "minimum_due_inr": inr(c["minimum_due"]),
        "due_date": c["due_date"],
    }


@fn
async def explain_bill_change(ctx: ToolContext, card_id: str | None = None) -> dict:
    c = await _card(ctx, card_id)
    sts = await ctx.get("cards", f"/cards/{c['card_id']}/statements", {"limit": 2}, cache_ttl_s=60)
    if len(sts) < 2:
        return {"card_id": c["card_id"], "note": "Only one statement so far; nothing to compare."}
    cur, prev = sts[0], sts[1]
    txns = {}
    for st in (cur, prev):
        txns[st["statement_id"]] = await ctx.get(
            "cards",
            f"/cards/{c['card_id']}/transactions",
            {"statement_id": st["statement_id"]},
            cache_ttl_s=60,
        )

    def by_category(stmt_id: str) -> dict:
        out = defaultdict(float)
        for t in txns[stmt_id]:
            if t["type"] != "payment":
                out[t["category"]] += t["amount"]
        return out

    now_cat, prev_cat = by_category(cur["statement_id"]), by_category(prev["statement_id"])
    changes = sorted(
        (
            {
                "category": k,
                "this_statement_inr": inr(now_cat.get(k, 0)),
                "previous_inr": inr(prev_cat.get(k, 0)),
                "change": now_cat.get(k, 0) - prev_cat.get(k, 0),
                "change_inr": inr(now_cat.get(k, 0) - prev_cat.get(k, 0)),
            }
            for k in set(now_cat) | set(prev_cat)
        ),
        key=lambda x: -abs(x["change"]),
    )
    # Possible duplicates: same merchant, amount and date in the latest statement.
    seen, duplicates = {}, []
    for t in txns[cur["statement_id"]]:
        if t["type"] != "purchase":
            continue
        key = (t["merchant"], t["amount"], t["txn_date"])
        if key in seen:
            duplicates.append(
                {
                    "txn_id": t["txn_id"],
                    "merchant": t["merchant"],
                    "date": t["txn_date"],
                    "amount_inr": inr(t["amount"]),
                    "first_txn_id": seen[key],
                }
            )
        seen[key] = t["txn_id"]
    big = sorted(
        (t for t in txns[cur["statement_id"]] if t["type"] in ("purchase", "fee")),
        key=lambda t: -t["amount"],
    )[:3]
    clauses = [await ctx.clause("CC-3.2")] if now_cat.get("fees") else []
    if duplicates:
        clauses.append(await ctx.clause("CC-5.1"))
    return {
        "card_id": c["card_id"],
        "last4": c["last4"],
        "this_statement": {
            "period": f"{cur['period_start']} to {cur['period_end']}",
            "balance_inr": inr(cur["statement_balance"]),
            "due_date": cur["due_date"],
        },
        "previous_statement": {
            "period": f"{prev['period_start']} to {prev['period_end']}",
            "balance_inr": inr(prev["statement_balance"]),
        },
        "increase_inr": inr(cur["statement_balance"] - prev["statement_balance"]),
        "biggest_changes_by_category": changes[:4],
        "largest_items": [
            {"merchant": t["merchant"], "date": t["txn_date"], "amount_inr": inr(t["amount"])}
            for t in big
        ],
        "possible_duplicate_charges": duplicates,
        "clauses": clauses,
    }


@fn
async def simulate_minimum_payment(
    ctx: ToolContext, card_id: str | None = None, amount: float | None = None
) -> dict:
    c = await _card(ctx, card_id)
    paid = c["minimum_due"] if amount is None else amount
    cost = revolving_interest(c["statement_balance"], paid, c["monthly_interest_pct"])
    verdict = ctx.evaluate("revolving", {"card": c, "paid": paid})
    return {
        "card_id": c["card_id"],
        "paying_inr": inr(paid),
        "statement_balance_inr": inr(c["statement_balance"]),
        "carried_forward_inr": inr(cost["carried_forward"]),
        "estimated_interest_next_statement_inr": inr(cost["interest"]),
        "gst_on_interest_inr": inr(cost["gst_on_interest"]),
        "estimated_total_cost_inr": inr(cost["total_cost"]),
        "monthly_rate_pct": c["monthly_interest_pct"],
        "assumes": "one month on the unpaid balance; new purchases would also attract interest from their date",
        "rule": verdict,
        "clause": await ctx.clause(verdict["clause"]),
    }


@fn
async def check_annual_fee(ctx: ToolContext, card_id: str | None = None) -> dict:
    c = await _card(ctx, card_id)
    verdict = ctx.evaluate("annual_fee", {"card": c})
    return {
        "card_id": c["card_id"],
        "annual_fee_inr": inr(c["annual_fee"]),
        "spend_last_12_months_inr": inr(c["spend_last_12m"]),
        "waiver_threshold_inr": inr(c["annual_fee_waiver_spend"]),
        "spend_still_needed_for_waiver_inr": inr(
            max(c["annual_fee_waiver_spend"] - c["spend_last_12m"], 0)
        ),
        "rule": verdict,
        "clause": await ctx.clause(verdict["clause"]),
    }


# --- Action prepare functions ---------------------------------------------------


@fn
async def prepare_card_dispute(ctx: ToolContext, txn_id: str, reason: str) -> dict:
    t = await ctx.get("cards", f"/transactions/{txn_id}")
    verdict = ctx.evaluate(
        "dispute",
        {"txn": t, "days_since_txn": (ctx.today - date.fromisoformat(t["txn_date"])).days},
    )
    if not verdict["eligible"]:
        from core.actions import ActionError

        raise ActionError(f"this transaction can't be disputed: {verdict['explain']}")
    return {
        "details": {
            "txn_id": txn_id,
            "card_id": t["card_id"],
            "merchant": t["merchant"],
            "txn_date": t["txn_date"],
            "amount": t["amount"],
            "customer_reason": reason,
        },
        "display": {
            "merchant": t["merchant"],
            "txn_date": nice_date(t["txn_date"]),
            "amount_inr": inr(t["amount"]),
        },
    }


@fn
async def prepare_card_block(ctx: ToolContext, card_id: str, reason: str) -> dict:
    c = await _card(ctx, card_id)
    return {
        "details": {"card_id": c["card_id"], "last4": c["last4"], "reason": reason},
        "display": {"last4": c["last4"]},
    }
