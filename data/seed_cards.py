"""Credit-card data for the second domain pack (called from data/seed.py).

Demo card for C1001, Platinum Rewards ending 4417:
- September statement Rs 21,880, paid in full on 18 October.
- October statement Rs 43,086 (due 20 November): higher because of a Rs 24,999
  electronics purchase, the annual fee plus GST (Rs 3,539) and a duplicate
  Rs 1,299 subscription charge, the dispute candidate for journey J5.

Generated cards (other customers) replay six months of a real, anonymised credit-card
account from the Home Credit Default Risk data (data/open/home_credit_card_months.csv):
balance, spending and repayments as shares of the credit limit, scaled to the card's
limit. Merchants and dates are generated; the amounts follow the real account.
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

REAL_CARDS = Path(__file__).resolve().parent / "open" / "home_credit_card_months.csv"
MONTHS = [
    ("2026-05", 31),
    ("2026-06", 30),
    ("2026-07", 31),
    ("2026-08", 31),
    ("2026-09", 30),
    ("2026-10", 31),
]


def real_card_months() -> list[list[dict]]:
    accounts: dict[str, list[dict]] = {}
    with REAL_CARDS.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            accounts.setdefault(r["account"], []).append(r)
    return [
        sorted(v, key=lambda r: int(r["month"]))
        for _, v in sorted(accounts.items(), key=lambda kv: int(kv[0]))
    ]


SCHEMA = """
CREATE TABLE cards (
    card_id TEXT PRIMARY KEY, customer_id TEXT, product TEXT, last4 TEXT, credit_limit REAL,
    issued TEXT, annual_fee REAL, annual_fee_waiver_spend REAL, spend_last_12m REAL,
    monthly_interest_pct REAL, statement_day INTEGER, due_days INTEGER
);
CREATE TABLE card_statements (
    statement_id TEXT PRIMARY KEY, card_id TEXT, customer_id TEXT, period_start TEXT, period_end TEXT,
    statement_date TEXT, due_date TEXT, opening_balance REAL, purchases REAL, fees REAL, interest REAL,
    payments REAL, statement_balance REAL, minimum_due REAL, paid_amount REAL, paid_on TEXT
);
CREATE TABLE card_transactions (
    txn_id TEXT PRIMARY KEY, card_id TEXT, customer_id TEXT, txn_date TEXT, merchant TEXT,
    category TEXT, amount REAL, type TEXT, statement_id TEXT
);
"""

SEP = [
    ("2026-09-04", "Swiggy", "food", 1120),
    ("2026-09-07", "BigBasket", "groceries", 3880),
    ("2026-09-05", "QuickRide Plus subscription", "subscriptions", 1299),
    ("2026-09-11", "HP Petrol", "fuel", 2600),
    ("2026-09-15", "Myntra", "shopping", 6499),
    ("2026-09-19", "Zomato", "food", 890),
    ("2026-09-24", "Amazon", "shopping", 5592),
]
OCT = [
    ("2026-10-03", "Swiggy", "food", 642, "purchase"),
    ("2026-10-05", "QuickRide Plus subscription", "subscriptions", 1299, "purchase"),
    ("2026-10-05", "QuickRide Plus subscription", "subscriptions", 1299, "purchase"),
    ("2026-10-09", "BigBasket", "groceries", 3412, "purchase"),
    ("2026-10-12", "Croma Electronics", "electronics", 24999, "purchase"),
    ("2026-10-14", "Indian Oil", "fuel", 2850, "purchase"),
    ("2026-10-18", "Payment received, thank you", "payment", -21880, "payment"),
    ("2026-10-21", "Annual membership fee", "fees", 2999, "fee"),
    ("2026-10-21", "GST on annual fee", "fees", 540, "fee"),
    ("2026-10-26", "Amazon", "shopping", 4560, "purchase"),
    ("2026-10-29", "Uber", "travel", 486, "purchase"),
]


def minimum_due(balance: float) -> float:
    return min(balance, max(round(balance * 0.05), 200)) if balance > 0 else 0.0


def seed_cards(con, today: date, rng: random.Random, customer_ids: list[str]) -> None:
    con.executescript(SCHEMA)

    def card(card_id, cid, last4, limit, issued, spend):
        con.execute(
            "INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                card_id,
                cid,
                "Platinum Rewards Credit Card",
                last4,
                limit,
                issued,
                2999,
                200000,
                spend,
                3.75,
                30,
                20,
            ),
        )

    def statement(card_id, cid, sid, start, end, opening, txns, paid, paid_on):
        purchases = sum(a for _, _, _, a, t in txns if t == "purchase")
        fees = sum(a for _, _, _, a, t in txns if t == "fee")
        interest = sum(a for _, _, _, a, t in txns if t == "interest")
        payments = -sum(a for _, _, _, a, t in txns if t == "payment")
        balance = round(opening + purchases + fees + interest - payments, 2)
        due = (date.fromisoformat(end) + timedelta(days=20)).isoformat()
        con.execute(
            "INSERT INTO card_statements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid,
                card_id,
                cid,
                start,
                end,
                end,
                due,
                opening,
                purchases,
                fees,
                interest,
                payments,
                balance,
                minimum_due(balance),
                paid,
                paid_on,
            ),
        )
        for i, (d, merchant, cat, amount, typ) in enumerate(txns):
            con.execute(
                "INSERT INTO card_transactions VALUES (?,?,?,?,?,?,?,?,?)",
                (f"T-{sid[3:]}-{i:02d}", card_id, cid, d, merchant, cat, amount, typ, sid),
            )
        return balance

    # Demo card
    card("CC-7781", "C1001", "4417", 200000, "2021-10-21", 146300)
    sep_balance = statement(
        "CC-7781",
        "C1001",
        "ST-7781-2609",
        "2026-09-01",
        "2026-09-30",
        0.0,
        [(d, m, c, a, "purchase") for d, m, c, a in SEP],
        21880,
        "2026-10-18",
    )
    statement(
        "CC-7781", "C1001", "ST-7781-2610", "2026-10-01", "2026-10-31", sep_balance, OCT, 0, None
    )

    # Generated cards for some other customers
    merchants = [
        ("Swiggy", "food"),
        ("Amazon", "shopping"),
        ("BigBasket", "groceries"),
        ("Indian Oil", "fuel"),
        ("Uber", "travel"),
        ("Flipkart", "shopping"),
    ]
    real = real_card_months()
    for n, cid in enumerate(customer_ids[3:18]):
        cid_card = f"CC-{8000 + n}"
        limit = rng.choice([50000, 100000, 150000])
        card(
            cid_card,
            cid,
            f"{rng.randint(1000, 9999)}",
            limit,
            "2023-05-10",
            rng.randint(40000, 260000),
        )
        months = real[n % len(real)]
        balance = 0.0
        for (ym, days), m in zip(MONTHS, months, strict=True):
            target = round(float(m["balance_share"]) * limit)
            spend = round(float(m["drawings_share"]) * limit)
            paid = min(round(float(m["payment_share"]) * limit), round(balance + spend))
            # Whatever the real balance has beyond spending minus payments is interest
            # and charges; if it is lower, the customer paid more than the payment share.
            rest = target - (balance + spend - paid)
            txns = []
            remaining, parts = spend, rng.randint(1, 5) if spend else 0
            for p in range(parts):
                amount = remaining if p == parts - 1 else round(remaining * rng.uniform(0.2, 0.6))
                remaining -= amount
                if amount > 0:
                    txns.append(
                        (
                            f"{ym}-{rng.randint(1, days):02d}",
                            *rng.choice(merchants),
                            amount,
                            "purchase",
                        )
                    )
            if rest < 0:
                paid += -rest
                rest = 0
            if paid > 0:
                txns.append(
                    (
                        f"{ym}-{min(18 + int(m['days_past_due']), days):02d}",
                        "Payment received, thank you",
                        "payment",
                        -paid,
                        "payment",
                    )
                )
            if rest > 0:
                txns.append(
                    (f"{ym}-{days:02d}", "Interest and charges", "interest", rest, "interest")
                )
            balance = statement(
                cid_card,
                cid,
                f"ST-{8000 + n}-{ym[2:4]}{ym[5:]}",
                f"{ym}-01",
                f"{ym}-{days:02d}",
                balance,
                sorted(txns),
                None,
                None,
            )
