"""Create data/bank.db with deterministic synthetic data.

Run: poetry run poe seed

The same seed always produces the same database, so every demo starts from the
same state and every answer can be checked by hand. "Today" is fixed by
DEMO_TODAY (default 2026-11-11).

Demo customers
- C1001 Priya Sharma (main demo customer)
    L-1042 floating-rate personal loan; benchmark rose, rate reset 10.25% -> 10.75%
           from 15 Oct 2026, so the 5 Nov EMI is higher than the 5 Oct EMI (J1, J4).
           Three failed EMI debits in the last six months, each a different cause (J2).
    L-1043 fixed-rate personal loan disbursed May 2026, inside its 12-month lock-in,
           so prepayment carries a charge, unlike L-1042 (J3).
- C1002 Rahul Verma: floating-rate loan, clean payment history.
- C1003 Ananya Iyer: fixed-rate loan past its lock-in.
- 47 more generated customers. Their names, contacts and loan terms are generated;
  their repayment behaviour is real: each loan replays an anonymised instalment
  history from the Home Credit Default Risk data (data/open/, see README there),
  so when and how late they paid follows real borrowers.
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data"))

import csv  # noqa: E402

from faker import Faker  # noqa: E402

from core.calculators import emi  # noqa: E402
from core.settings import get_settings  # noqa: E402

SCHEMA = """
CREATE TABLE customers (
    customer_id TEXT PRIMARY KEY, name TEXT, phone TEXT, email TEXT, city TEXT,
    kyc_status TEXT, language TEXT, segment TEXT, customer_since TEXT
);
CREATE TABLE interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT, ts TEXT, channel TEXT, summary TEXT
);
CREATE TABLE loans (
    loan_id TEXT PRIMARY KEY, customer_id TEXT, product TEXT, purpose TEXT, borrower_type TEXT,
    principal REAL, disbursal_date TEXT, tenure_months INTEGER, rate_type TEXT,
    benchmark TEXT, spread_pct REAL, emi_day INTEGER, status TEXT, mandate_status TEXT
);
CREATE TABLE rate_history (
    loan_id TEXT, effective_date TEXT, rate_pct REAL, benchmark_rate_pct REAL, reason TEXT
);
CREATE TABLE installments (
    loan_id TEXT, installment_no INTEGER, due_date TEXT, rate_pct REAL, emi REAL,
    interest REAL, principal REAL, opening REAL, closing REAL,
    PRIMARY KEY (loan_id, installment_no)
);
CREATE TABLE payments (
    payment_id TEXT PRIMARY KEY, loan_id TEXT, customer_id TEXT, installment_no INTEGER,
    due_date TEXT, attempt_date TEXT, amount REAL, status TEXT, failure_reason TEXT,
    failure_code TEXT, mode TEXT, charge REAL, retry_of TEXT
);
CREATE TABLE service_requests (
    request_id TEXT PRIMARY KEY, customer_id TEXT, type TEXT, status TEXT, details TEXT,
    idempotency_key TEXT UNIQUE, created_at TEXT, sla_due TEXT, channel TEXT
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, customer_id TEXT, action TEXT,
    params TEXT, outcome TEXT, idempotency_key TEXT
);
"""


def add_months(d: date, months: int, day: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(day, monthrange(y, m)[1]))


def build_installments(loan: dict, resets: list[tuple[date, float]]) -> list[dict]:
    """Full schedule. On each reset the EMI is recalculated for the remaining tenure."""
    disb = date.fromisoformat(loan["disbursal_date"])
    n = loan["tenure_months"]
    balance = loan["principal"]
    rate = resets[0][1]
    payment = emi(balance, rate, n)
    rows = []
    for k in range(1, n + 1):
        due = add_months(disb, k, loan["emi_day"])
        # Apply any reset effective on or before the previous due date's period.
        new_rate = rate
        for eff, r in resets:
            if eff <= due - timedelta(days=1):
                new_rate = r
        if new_rate != rate:
            rate = new_rate
            payment = emi(balance, rate, n - k + 1)
        interest = balance * rate / 1200
        pay = min(payment, balance + interest)
        principal = pay - interest
        closing = max(balance - principal, 0.0)
        rows.append(
            dict(
                loan_id=loan["loan_id"],
                installment_no=k,
                due_date=due.isoformat(),
                rate_pct=rate,
                emi=round(pay, 2),
                interest=round(interest, 2),
                principal=round(principal, 2),
                opening=round(balance, 2),
                closing=round(closing, 2),
            )
        )
        balance = closing
    return rows


OPEN_DATA = ROOT / "data" / "open"


def real_loan_histories() -> list[dict[int, int]]:
    """Per real loan: instalment number -> days late (<= 0 is on time)."""
    histories: dict[str, dict[int, int]] = {}
    with (OPEN_DATA / "home_credit_loan_histories.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            histories.setdefault(r["history"], {})[int(r["instalment"])] = int(r["days_late"])
    return [histories[k] for k in sorted(histories, key=int)]


def failure_for_lateness(days_late: int) -> tuple | None:
    """The dataset records when an instalment was paid, not why it was late; the
    reason is our mapping, consistent with policy PAY-2.1 (no charge for a bank-side
    technical error)."""
    if days_late <= 0:
        return None
    if days_late <= 3:
        return (
            "Technical error at the customer's bank",
            "BANK_TECHNICAL",
            days_late,
            "NACH retry",
            0.0,
        )
    if days_late <= 10:
        return (
            "Insufficient funds in the linked account",
            "INSUFFICIENT_FUNDS",
            days_late,
            "UPI",
            500.0,
        )
    return ("NACH auto-debit mandate expired", "MANDATE_EXPIRED", days_late, "Net banking", 500.0)


def insert(con, table: str, row: dict) -> None:
    cols = ", ".join(row)
    con.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values())
    )


def seed(db_path: Path, today: date) -> dict:
    rng = random.Random(42)
    fake = Faker("en_IN")
    Faker.seed(42)
    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)

    customers, loans = [], []

    def customer(cid, name, phone, email, city, since):
        c = dict(
            customer_id=cid,
            name=name,
            phone=phone,
            email=email,
            city=city,
            kyc_status="verified",
            language="en-IN",
            segment="retail",
            customer_since=since,
        )
        customers.append(c)
        return c

    def loan(
        lid,
        cid,
        product,
        principal,
        disb,
        tenure,
        rate_type,
        spread,
        emi_day,
        resets,
        purpose="personal",
    ):
        lo = dict(
            loan_id=lid,
            customer_id=cid,
            product=product,
            purpose=purpose,
            borrower_type="individual",
            principal=principal,
            disbursal_date=disb.isoformat(),
            tenure_months=tenure,
            rate_type=rate_type,
            benchmark="RBI repo rate" if rate_type == "floating" else None,
            spread_pct=spread,
            emi_day=emi_day,
            status="active",
            mandate_status="active",
        )
        loans.append((lo, resets))
        return lo

    # --- Hand-built demo customers -------------------------------------------------
    customer(
        "C1001",
        "Priya Sharma",
        "+91 98450 12345",
        "priya.sharma@example.in",
        "Bengaluru",
        "2021-03-14",
    )
    loan(
        "L-1042",
        "C1001",
        "Personal Loan (floating rate)",
        600_000,
        date(2024, 12, 5),
        60,
        "floating",
        4.25,
        5,
        [
            (date(2024, 12, 5), 10.50, 6.25, "rate at sanction"),
            (date(2025, 6, 1), 10.25, 6.00, "repo rate cut by 25 bps"),
            (date(2026, 10, 15), 10.75, 6.50, "repo rate raised by 50 bps"),
        ],
    )
    loan(
        "L-1043",
        "C1001",
        "Personal Loan (fixed rate)",
        200_000,
        date(2026, 5, 20),
        36,
        "fixed",
        None,
        20,
        [(date(2026, 5, 20), 13.50, None, "fixed rate at sanction")],
    )
    customer(
        "C1002", "Rahul Verma", "+91 99000 54321", "rahul.verma@example.in", "Pune", "2019-08-02"
    )
    loan(
        "L-2077",
        "C1002",
        "Personal Loan (floating rate)",
        800_000,
        date(2025, 2, 10),
        48,
        "floating",
        4.25,
        10,
        [
            (date(2025, 2, 10), 10.50, 6.25, "rate at sanction"),
            (date(2025, 6, 1), 10.25, 6.00, "repo rate cut by 25 bps"),
            (date(2026, 10, 15), 10.75, 6.50, "repo rate raised by 50 bps"),
        ],
    )
    customer(
        "C1003", "Ananya Iyer", "+91 98860 67890", "ananya.iyer@example.in", "Chennai", "2022-11-21"
    )
    loan(
        "L-3015",
        "C1003",
        "Personal Loan (fixed rate)",
        350_000,
        date(2024, 9, 15),
        48,
        "fixed",
        None,
        15,
        [(date(2024, 9, 15), 12.75, None, "fixed rate at sanction")],
    )

    # --- Generated customers -------------------------------------------------------
    for i in range(47):
        cid = f"C{2000 + i}"
        name = fake.name()
        customer(
            cid,
            name,
            fake.phone_number(),
            f"{name.split()[0].lower()}{i}@example.in",
            fake.city(),
            fake.date_between(date(2015, 1, 1), date(2024, 1, 1)).isoformat(),
        )
        for j in range(rng.choice([1, 1, 1, 2])):
            disb = date(2023, 1, 1) + timedelta(days=rng.randint(0, 1000))
            if disb > today - timedelta(days=60):
                disb = today - timedelta(days=200)
            floating = rng.random() < 0.5
            amount = rng.choice([100_000, 150_000, 250_000, 300_000, 500_000, 750_000, 1_000_000])
            tenure = rng.choice([24, 36, 48, 60])
            day = rng.choice([1, 5, 10, 15, 20])
            if floating:
                resets = [(disb, 10.50, 6.25, "rate at sanction")]
                if disb < date(2025, 6, 1):
                    resets.append((date(2025, 6, 1), 10.25, 6.00, "repo rate cut by 25 bps"))
                resets.append(
                    (
                        date(2026, 10, 15),
                        10.75 if disb < date(2025, 6, 1) else 11.00,
                        6.50,
                        "repo rate raised by 50 bps",
                    )
                )
                resets = [r for r in resets if r[0] >= disb]
                loan(
                    f"L-{5000 + i * 2 + j}",
                    cid,
                    "Personal Loan (floating rate)",
                    amount,
                    disb,
                    tenure,
                    "floating",
                    4.25,
                    day,
                    resets,
                )
            else:
                rate = rng.choice([11.5, 12.25, 12.75, 13.5, 14.0])
                loan(
                    f"L-{5000 + i * 2 + j}",
                    cid,
                    "Personal Loan (fixed rate)",
                    amount,
                    disb,
                    tenure,
                    "fixed",
                    None,
                    day,
                    [(disb, rate, None, "fixed rate at sanction")],
                )

    for c in customers:
        insert(con, "customers", c)

    # --- Schedules, payments, failures ---------------------------------------------
    demo_failures = {
        # (loan, due date): (reason, code, retry offset days, retry mode, charge)
        ("L-1042", "2026-06-05"): (
            "Insufficient funds in the linked account",
            "INSUFFICIENT_FUNDS",
            4,
            "UPI",
            500.0,
        ),
        ("L-1042", "2026-08-05"): (
            "NACH auto-debit mandate expired",
            "MANDATE_EXPIRED",
            7,
            "Net banking",
            500.0,
        ),
        ("L-1042", "2026-09-05"): (
            "Technical error at the customer's bank",
            "BANK_TECHNICAL",
            1,
            "NACH retry",
            0.0,
        ),
    }
    pay_seq = 0
    histories = real_loan_histories()
    generated = [
        lo["loan_id"] for lo, _ in loans if lo["customer_id"] not in ("C1001", "C1002", "C1003")
    ]
    history_of = {loan_id: histories[k % len(histories)] for k, loan_id in enumerate(generated)}
    for lo, resets in loans:
        insert(con, "loans", lo)
        for eff, r, bench, reason in resets:
            insert(
                con,
                "rate_history",
                dict(
                    loan_id=lo["loan_id"],
                    effective_date=eff.isoformat(),
                    rate_pct=r,
                    benchmark_rate_pct=bench,
                    reason=reason,
                ),
            )
        rows = build_installments(lo, [(eff, r) for eff, r, _, _ in resets])
        for row in rows:
            insert(con, "installments", row)
            if row["due_date"] > today.isoformat():
                continue
            pay_seq += 1
            key = (lo["loan_id"], row["due_date"])
            failure = demo_failures.get(key)
            if failure is None and lo["loan_id"] in history_of:
                failure = failure_for_lateness(
                    history_of[lo["loan_id"]].get(row["installment_no"], 0)
                )
            base = dict(
                loan_id=lo["loan_id"],
                customer_id=lo["customer_id"],
                installment_no=row["installment_no"],
                due_date=row["due_date"],
                amount=row["emi"],
            )
            if failure:
                reason, code, offset, mode, charge = failure
                failed_id = f"P{pay_seq:06d}F"
                insert(
                    con,
                    "payments",
                    dict(
                        base,
                        payment_id=failed_id,
                        attempt_date=row["due_date"],
                        status="failed",
                        failure_reason=reason,
                        failure_code=code,
                        mode="NACH auto-debit",
                        charge=charge,
                        retry_of=None,
                    ),
                )
                paid = date.fromisoformat(row["due_date"]) + timedelta(days=offset)
                insert(
                    con,
                    "payments",
                    dict(
                        base,
                        payment_id=f"P{pay_seq:06d}",
                        attempt_date=paid.isoformat(),
                        status="success",
                        failure_reason=None,
                        failure_code=None,
                        mode=mode,
                        charge=0.0,
                        retry_of=failed_id,
                    ),
                )
            else:
                insert(
                    con,
                    "payments",
                    dict(
                        base,
                        payment_id=f"P{pay_seq:06d}",
                        attempt_date=row["due_date"],
                        status="success",
                        failure_reason=None,
                        failure_code=None,
                        mode="NACH auto-debit",
                        charge=0.0,
                        retry_of=None,
                    ),
                )

    # L-1042's mandate expired in August and was renewed in September.
    con.execute(
        "UPDATE loans SET mandate_status='active (renewed 2026-09-02)' WHERE loan_id='L-1042'"
    )

    for cid, ts, ch, summary in [
        (
            "C1001",
            "2026-08-07T10:12:00",
            "phone",
            "Asked why the August EMI debit failed; told the mandate expired.",
        ),
        ("C1001", "2026-09-02T16:40:00", "app", "Registered a new NACH mandate for L-1042."),
        (
            "C1001",
            "2026-10-16T09:05:00",
            "sms",
            "Notified of rate reset on L-1042 from 10.25% to 10.75%.",
        ),
    ]:
        insert(con, "interactions", dict(customer_id=cid, ts=ts, channel=ch, summary=summary))

    # Second domain pack: credit cards.
    from seed_cards import seed_cards

    seed_cards(con, today, rng, [c["customer_id"] for c in customers])

    con.commit()
    counts = {
        t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("customers", "loans", "installments", "payments", "cards", "card_transactions")
    }
    con.close()
    return counts


def main() -> int:
    s = get_settings()
    path = ROOT / s.db_path
    counts = seed(path, date.fromisoformat(s.demo_today))
    print(f"Seeded {path.relative_to(ROOT)} for today={s.demo_today}: {json.dumps(counts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
