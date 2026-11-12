"""Extract a small sample of real repayment behaviour from the Home Credit Default Risk data.

Source: Home Credit Default Risk (Kaggle competition, 2018), anonymised data on real
consumer-credit customers, read from the public Hugging Face mirror
`mohameddhameem/home-credit-default-risk`. Only behaviour ratios are kept: no ids,
amounts or dates leave this script, so the sample cannot be linked back to anyone.

Writes (committed, so seeding needs no network or extra packages):
- data/open/home_credit_loan_histories.csv: per instalment of real loans, how many days
  late it was paid and what share of the instalment was paid
- data/open/home_credit_card_months.csv: six consecutive months of real credit-card
  accounts, as shares of the credit limit (balance, drawings, payments, minimum
  instalment) and days past due

data/seed.py maps these onto the generated customers' loans and cards.

Run once (downloads about 350 MB into data/open/cache, which is git-ignored):
    poetry install --with evals
    poetry run python data/open/extract_home_credit.py
"""

from __future__ import annotations

import csv
import hashlib
import sys
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
MIRROR = "https://huggingface.co/datasets/mohameddhameem/home-credit-default-risk/resolve/main"
FILES = {
    "installments": "installments_payments/train-00000-of-00002.parquet",
    "cards": "credit_card_balance/train-00000-of-00002.parquet",
}
LOANS = 80  # loan histories kept
CARDS = 30  # card accounts kept
MIN_INSTALMENTS, MAX_INSTALMENTS = 24, 60


def fetch(key: str) -> Path:
    CACHE.mkdir(exist_ok=True)
    f = CACHE / f"{key}.parquet"
    if not f.exists():
        print(f"Downloading {FILES[key]} ...", flush=True)
        with httpx.stream("GET", f"{MIRROR}/{FILES[key]}", follow_redirects=True, timeout=600) as r:
            r.raise_for_status()
            with f.open("wb") as out:
                for chunk in r.iter_bytes():
                    out.write(chunk)
    return f


def pick(ids: list[int], n: int, salt: str) -> list[int]:
    """Deterministic, unbiased sample: order ids by a hash, take the first n."""
    return sorted(ids, key=lambda i: hashlib.sha256(f"{salt}{i}".encode()).hexdigest())[:n]


def loan_histories() -> list[dict]:
    t = pq.read_table(
        fetch("installments"),
        columns=[
            "SK_ID_PREV",
            "NUM_INSTALMENT_VERSION",
            "NUM_INSTALMENT_NUMBER",
            "DAYS_INSTALMENT",
            "DAYS_ENTRY_PAYMENT",
            "AMT_INSTALMENT",
            "AMT_PAYMENT",
        ],
    )
    # Regular instalments only (version 1 = the original schedule), with a payment.
    t = t.filter(
        pc.and_(
            pc.equal(t["NUM_INSTALMENT_VERSION"], 1),
            pc.and_(pc.greater(t["AMT_INSTALMENT"], 0), pc.is_valid(t["DAYS_ENTRY_PAYMENT"])),
        )
    )
    g = t.group_by("SK_ID_PREV").aggregate(
        [
            ("NUM_INSTALMENT_NUMBER", "count"),
            ("NUM_INSTALMENT_NUMBER", "count_distinct"),
            ("NUM_INSTALMENT_NUMBER", "max"),
        ]
    )
    # One row per instalment, a complete run 1..n, and a loan long enough to use.
    ok = pc.and_(
        pc.and_(
            pc.equal(g["NUM_INSTALMENT_NUMBER_count"], g["NUM_INSTALMENT_NUMBER_max"]),
            pc.equal(g["NUM_INSTALMENT_NUMBER_count_distinct"], g["NUM_INSTALMENT_NUMBER_max"]),
        ),
        pc.and_(
            pc.greater_equal(g["NUM_INSTALMENT_NUMBER_count"], MIN_INSTALMENTS),
            pc.less_equal(g["NUM_INSTALMENT_NUMBER_count"], MAX_INSTALMENTS),
        ),
    )
    ids = pick(g.filter(ok)["SK_ID_PREV"].to_pylist(), LOANS, "loan")
    rows = t.filter(pc.is_in(t["SK_ID_PREV"], value_set=pa.array(ids))).to_pylist()
    rows.sort(key=lambda r: (ids.index(r["SK_ID_PREV"]), r["NUM_INSTALMENT_NUMBER"]))
    return [
        {
            "history": ids.index(r["SK_ID_PREV"]) + 1,
            "instalment": int(r["NUM_INSTALMENT_NUMBER"]),
            "days_late": int(r["DAYS_ENTRY_PAYMENT"] - r["DAYS_INSTALMENT"]),
            "paid_ratio": round(r["AMT_PAYMENT"] / r["AMT_INSTALMENT"], 3),
        }
        for r in rows
    ]


def card_months() -> list[dict]:
    t = pq.read_table(
        fetch("cards"),
        columns=[
            "SK_ID_PREV",
            "MONTHS_BALANCE",
            "AMT_BALANCE",
            "AMT_CREDIT_LIMIT_ACTUAL",
            "AMT_DRAWINGS_CURRENT",
            "AMT_PAYMENT_TOTAL_CURRENT",
            "AMT_INST_MIN_REGULARITY",
            "SK_DPD",
        ],
    )
    # The last six months of each account, with a credit limit and a non-zero balance.
    t = t.filter(
        pc.and_(
            pc.greater_equal(t["MONTHS_BALANCE"], -6),
            pc.greater(t["AMT_CREDIT_LIMIT_ACTUAL"], 0),
        )
    )
    g = t.group_by("SK_ID_PREV").aggregate(
        [("MONTHS_BALANCE", "count"), ("AMT_BALANCE", "max"), ("AMT_DRAWINGS_CURRENT", "max")]
    )
    ok = pc.and_(
        pc.equal(g["MONTHS_BALANCE_count"], 6),
        pc.and_(pc.greater(g["AMT_BALANCE_max"], 0), pc.greater(g["AMT_DRAWINGS_CURRENT_max"], 0)),
    )
    ids = pick(g.filter(ok)["SK_ID_PREV"].to_pylist(), CARDS, "card")
    rows = t.filter(pc.is_in(t["SK_ID_PREV"], value_set=pa.array(ids))).to_pylist()
    rows.sort(key=lambda r: (ids.index(r["SK_ID_PREV"]), r["MONTHS_BALANCE"]))

    def share(v, limit):
        return round(min(max((v or 0.0) / limit, 0.0), 1.5), 4)

    return [
        {
            "account": ids.index(r["SK_ID_PREV"]) + 1,
            "month": 7 + int(r["MONTHS_BALANCE"]),  # 1 (oldest) .. 6 (latest)
            "balance_share": share(r["AMT_BALANCE"], r["AMT_CREDIT_LIMIT_ACTUAL"]),
            "drawings_share": share(r["AMT_DRAWINGS_CURRENT"], r["AMT_CREDIT_LIMIT_ACTUAL"]),
            "payment_share": share(r["AMT_PAYMENT_TOTAL_CURRENT"], r["AMT_CREDIT_LIMIT_ACTUAL"]),
            "min_instalment_share": share(
                r["AMT_INST_MIN_REGULARITY"], r["AMT_CREDIT_LIMIT_ACTUAL"]
            ),
            "days_past_due": int(r["SK_DPD"] or 0),
        }
        for r in rows
    ]


def write(name: str, rows: list[dict]) -> None:
    with (HERE / name).open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to data/open/{name}")


def main() -> int:
    write("home_credit_loan_histories.csv", loan_histories())
    write("home_credit_card_months.csv", card_months())
    return 0


if __name__ == "__main__":
    sys.exit(main())
