"""Open-data seeding: the committed Home Credit sample loads, and its behaviour drives
the generated customers' payments and card statements."""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "data"))

from seed import failure_for_lateness, real_loan_histories  # noqa: E402
from seed_cards import real_card_months  # noqa: E402


def test_real_samples_load():
    loans = real_loan_histories()
    assert len(loans) == 80 and all(len(h) >= 24 for h in loans)
    cards = real_card_months()
    assert len(cards) == 30 and all(len(m) == 6 for m in cards)


def test_lateness_maps_to_a_failure_consistent_with_policy():
    assert failure_for_lateness(0) is None and failure_for_lateness(-5) is None
    reason, code, offset, _, charge = failure_for_lateness(2)
    assert code == "BANK_TECHNICAL" and charge == 0.0 and offset == 2  # PAY-2.1: no charge
    assert failure_for_lateness(7)[1] == "INSUFFICIENT_FUNDS"
    assert failure_for_lateness(20)[1] == "MANDATE_EXPIRED"


def test_seeded_generated_customers_follow_real_histories():
    db = sqlite3.connect(ROOT / "data" / "test.db")
    failed = db.execute(
        "SELECT count(*) FROM payments WHERE status='failed' AND customer_id LIKE 'C2%'"
    ).fetchone()[0]
    assert failed > 0
    months = db.execute("SELECT count(*) FROM card_statements WHERE card_id='CC-8000'").fetchone()[
        0
    ]
    assert months == 6
