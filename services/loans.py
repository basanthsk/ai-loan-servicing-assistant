"""Mock Loan Management system: active loans, balances, schedules, interest rates.

Loan endpoints take customer_id as a query parameter and refuse loans that
belong to someone else; the assistant's connector always sets it from the
session, never from the model.
"""

from datetime import date

from services.common import create_app, db, require_owner, rows, today

app = create_app("loans")


def loan_state(con, loan: dict) -> dict:
    """Current position of a loan as of the demo's today."""
    t = today().isoformat()
    lid = loan["loan_id"]
    last_paid = con.execute(
        "SELECT * FROM installments WHERE loan_id=? AND due_date<=? ORDER BY installment_no DESC LIMIT 1",
        (lid, t),
    ).fetchone()
    nxt = con.execute(
        "SELECT * FROM installments WHERE loan_id=? AND due_date>? ORDER BY installment_no LIMIT 1",
        (lid, t),
    ).fetchone()
    remaining = con.execute(
        "SELECT COUNT(*) FROM installments WHERE loan_id=? AND due_date>?", (lid, t)
    ).fetchone()[0]
    disb = date.fromisoformat(loan["disbursal_date"])
    months_since = (
        (today().year - disb.year) * 12 + today().month - disb.month - (today().day < disb.day)
    )
    outstanding = last_paid["closing"] if last_paid else loan["principal"]
    days_since_last = (today() - date.fromisoformat(last_paid["due_date"])).days if last_paid else 0
    return {
        **loan,
        "current_rate_pct": nxt["rate_pct"] if nxt else None,
        "outstanding_principal": round(outstanding, 2),
        "current_emi": nxt["emi"] if nxt else 0,
        "next_due_date": nxt["due_date"] if nxt else None,
        "remaining_installments": remaining,
        "installments_paid": last_paid["installment_no"] if last_paid else 0,
        "last_emi_date": last_paid["due_date"] if last_paid else None,
        "last_emi_amount": last_paid["emi"] if last_paid else None,
        "months_since_disbursal": months_since,
        "days_since_last_emi": days_since_last,
        "as_of": today().isoformat(),
    }


@app.get("/customers/{customer_id}/loans")
def customer_loans(customer_id: str):
    with db() as con:
        loans = rows(
            con.execute("SELECT * FROM loans WHERE customer_id=? ORDER BY loan_id", (customer_id,))
        )
        return [loan_state(con, lo) for lo in loans]


@app.get("/loans/{loan_id}")
def loan_detail(loan_id: str, customer_id: str):
    with db() as con:
        loan = require_owner(con, "loans", "loan_id", loan_id, customer_id)
        state = loan_state(con, loan)
        state["rate_history"] = rows(
            con.execute(
                "SELECT effective_date, rate_pct, benchmark_rate_pct, reason FROM rate_history "
                "WHERE loan_id=? ORDER BY effective_date",
                (loan_id,),
            )
        )
        # EMI amounts around today, so "why did my EMI change" can be answered from data.
        state["recent_installments"] = rows(
            con.execute(
                "SELECT installment_no, due_date, rate_pct, emi, interest, principal, closing "
                "FROM installments WHERE loan_id=? AND due_date<=? ORDER BY installment_no DESC LIMIT 4",
                (loan_id, today().isoformat()),
            )
        )[::-1]
        return state


@app.get("/loans/{loan_id}/schedule")
def schedule(loan_id: str, customer_id: str, upcoming: int = 6):
    with db() as con:
        require_owner(con, "loans", "loan_id", loan_id, customer_id)
        return rows(
            con.execute(
                "SELECT installment_no, due_date, rate_pct, emi, interest, principal, closing "
                "FROM installments WHERE loan_id=? AND due_date>? ORDER BY installment_no LIMIT ?",
                (loan_id, today().isoformat(), upcoming),
            )
        )
