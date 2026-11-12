"""Mock Payments system: payment history, payment status, failed transactions."""

from datetime import date, timedelta

from services.common import create_app, db, require_owner, rows, today

app = create_app("payments")


@app.get("/customers/{customer_id}/payments")
def customer_payments(
    customer_id: str,
    loan_id: str | None = None,
    status: str | None = None,
    months: int = 6,
    limit: int = 50,
):
    since = (today() - timedelta(days=round(months * 30.44))).isoformat()
    sql = "SELECT * FROM payments WHERE customer_id=? AND attempt_date>=? AND attempt_date<=?"
    args: list = [customer_id, since, today().isoformat()]
    if loan_id:
        sql += " AND loan_id=?"
        args.append(loan_id)
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY attempt_date DESC, payment_id DESC LIMIT ?"
    args.append(limit)
    with db() as con:
        return {
            "since": since,
            "until": today().isoformat(),
            "payments": rows(con.execute(sql, args)),
        }


@app.get("/payments/{payment_id}")
def payment(payment_id: str, customer_id: str):
    with db() as con:
        p = require_owner(con, "payments", "payment_id", payment_id, customer_id)
        if p["status"] == "failed":
            retry = con.execute("SELECT * FROM payments WHERE retry_of=?", (payment_id,)).fetchone()
            p["resolved_by"] = dict(retry) if retry else None
            p["days_late"] = (
                (date.fromisoformat(retry["attempt_date"]) - date.fromisoformat(p["due_date"])).days
                if retry
                else None
            )
        return p
