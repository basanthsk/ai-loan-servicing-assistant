"""Mock Customer CRM: profiles, contact details, interaction history."""

from fastapi import HTTPException

from services.common import create_app, db, rows

app = create_app("crm")


@app.get("/customers")
def list_customers(limit: int = 10):
    """Demo login list: the three hand-built customers first."""
    with db() as con:
        return rows(
            con.execute(
                "SELECT customer_id, name, city FROM customers ORDER BY customer_id LIMIT ?",
                (limit,),
            )
        )


@app.get("/customers/{customer_id}")
def get_customer(customer_id: str):
    with db() as con:
        row = con.execute("SELECT * FROM customers WHERE customer_id=?", (customer_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "customer not found")
    return dict(row)


@app.get("/customers/{customer_id}/interactions")
def interactions(customer_id: str, limit: int = 10):
    with db() as con:
        return rows(
            con.execute(
                "SELECT ts, channel, summary FROM interactions WHERE customer_id=? ORDER BY ts DESC LIMIT ?",
                (customer_id, limit),
            )
        )
