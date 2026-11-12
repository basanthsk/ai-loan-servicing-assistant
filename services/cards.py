"""Mock Cards system: card accounts, statements, transactions (second domain pack).

Writes still go through Service Ops. A card shows as blocked once Service Ops
has completed a card_block request for it, so a block raised by the assistant
is visible here straight away.
"""

import json

from services.common import create_app, db, require_owner, rows, today

app = create_app("cards")


def _status(con, card_id: str) -> str:
    for r in con.execute(
        "SELECT details FROM service_requests WHERE type='card_block' AND status='completed'"
    ).fetchall():
        if json.loads(r["details"]).get("card_id") == card_id:
            return "blocked"
    return "active"


def _summary(con, card: dict) -> dict:
    st = con.execute(
        "SELECT * FROM card_statements WHERE card_id=? AND statement_date<=? ORDER BY statement_date DESC LIMIT 1",
        (card["card_id"], today().isoformat()),
    ).fetchone()
    out = {**card, "status": _status(con, card["card_id"]), "as_of": today().isoformat()}
    if st:
        out.update(
            latest_statement_id=st["statement_id"],
            statement_date=st["statement_date"],
            statement_balance=st["statement_balance"],
            minimum_due=st["minimum_due"],
            due_date=st["due_date"],
            paid_amount=st["paid_amount"],
            available_credit=card["credit_limit"] - st["statement_balance"],
        )
    return out


@app.get("/customers/{customer_id}/cards")
def customer_cards(customer_id: str):
    with db() as con:
        cards = rows(
            con.execute("SELECT * FROM cards WHERE customer_id=? ORDER BY card_id", (customer_id,))
        )
        return [_summary(con, c) for c in cards]


@app.get("/cards/{card_id}")
def card_detail(card_id: str, customer_id: str):
    with db() as con:
        return _summary(con, require_owner(con, "cards", "card_id", card_id, customer_id))


@app.get("/cards/{card_id}/statements")
def statements(card_id: str, customer_id: str, limit: int = 2):
    with db() as con:
        require_owner(con, "cards", "card_id", card_id, customer_id)
        return rows(
            con.execute(
                "SELECT * FROM card_statements WHERE card_id=? AND statement_date<=? ORDER BY statement_date DESC LIMIT ?",
                (card_id, today().isoformat(), limit),
            )
        )


@app.get("/cards/{card_id}/transactions")
def transactions(card_id: str, customer_id: str, statement_id: str | None = None, limit: int = 50):
    sql = "SELECT * FROM card_transactions WHERE card_id=?"
    args: list = [card_id]
    if statement_id:
        sql += " AND statement_id=?"
        args.append(statement_id)
    sql += " ORDER BY txn_date DESC, txn_id LIMIT ?"
    args.append(limit)
    with db() as con:
        require_owner(con, "cards", "card_id", card_id, customer_id)
        return rows(con.execute(sql, args))


@app.get("/transactions/{txn_id}")
def transaction(txn_id: str, customer_id: str):
    with db() as con:
        return require_owner(con, "card_transactions", "txn_id", txn_id, customer_id)
