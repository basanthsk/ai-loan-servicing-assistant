"""Second domain pack (credit cards) on the unchanged platform, offline."""

from conftest import FakeChain, Recorder, text_reply, tool_reply

from core.session import Session
from packs.credit_cards.functions import minimum_due, revolving_interest


async def session_for(orch):
    s = Session(customer_id="C1001")
    await orch.prefetch(s)
    return s


def test_card_calculators():
    assert minimum_due(43086) == 2154
    assert minimum_due(1500) == 200
    assert minimum_due(150) == 150
    r = revolving_interest(43086, 2154, 3.75)
    assert r["carried_forward"] == 40932
    assert round(r["interest"], 2) == round(40932 * 0.0375, 2)


async def test_prefetch_includes_cards(make_orchestrator):
    orch = make_orchestrator(FakeChain([]))
    s = await session_for(orch)
    assert s.accounts["credit_cards"]["cards"][0]["last4"] == "4417"


async def test_card_minimum_due_fast_path(make_orchestrator):
    orch = make_orchestrator(FakeChain([], decisions={"card": "card_minimum_due"}))
    s = await session_for(orch)
    answer = await orch.handle(s, "How much do I owe on my card?", Recorder())
    assert "4417" in answer and "₹2,154" in answer and "20 November 2026" in answer


async def test_bill_change_finds_the_duplicate_charge(make_orchestrator):
    chain = FakeChain(
        [
            tool_reply(("explain_bill_change", {"card_id": "CC-7781"})),
            text_reply("Your bill rose to ₹43,086 from ₹21,880, mainly a ₹24,999 purchase."),
        ],
        decisions={"bill": "card_bill_change"},
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    rec = Recorder()
    await orch.handle(s, "Why is my card bill higher?", rec)
    assert rec.done["pack"] == "credit_cards" and rec.done["verified"]
    tool_msg = next(m for m in chain.planner_calls[-1] if m.get("role") == "tool")
    assert (
        "QuickRide Plus subscription" in tool_msg["content"]
        and "possible_duplicate_charges" in tool_msg["content"]
    )


async def test_card_dispute_needs_otp(make_orchestrator):
    chain = FakeChain(
        [
            tool_reply(
                (
                    "dispute_card_transaction",
                    {"txn_id": "T-7781-2610-02", "reason": "charged twice"},
                )
            ),
            text_reply("Shall I raise it?"),
        ],
        decisions={"dispute": "card_dispute"},
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    rec = Recorder()
    await orch.handle(s, "Please dispute the duplicate QuickRide charge", rec)
    assert "₹1,299" in rec.of("action_required")[0]["action"]["summary"]
    rec = Recorder()
    await orch.handle(s, "yes", rec)
    rec2 = Recorder()
    answer = await orch.handle(s, rec.of("otp_required")[0]["demo_code"], rec2)
    assert "CDS-" in answer


async def test_card_block_skips_otp_and_card_shows_blocked(make_orchestrator, system_apps):
    from fastapi.testclient import TestClient

    chain = FakeChain(
        [
            tool_reply(("block_card", {"card_id": "CC-7781", "reason": "lost"})),
            text_reply("Block it?"),
        ],
        decisions={"lost": "card_block"},
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    await orch.handle(s, "I lost my card", Recorder())
    rec = Recorder()
    answer = await orch.handle(s, "yes", rec)
    assert not rec.of("otp_required") and "CBK-" in answer
    cards = TestClient(system_apps["cards"]).get("/customers/C1001/cards").json()
    assert cards[0]["status"] == "blocked"


def test_old_transaction_cannot_be_disputed():
    from pathlib import Path

    from core.rules import RulesEngine

    rules = RulesEngine.from_file(
        Path(__file__).resolve().parents[1] / "packs/credit_cards/rules.yaml"
    )
    v = rules.evaluate("dispute", {"txn": {"type": "purchase"}, "days_since_txn": 90})
    assert v.values["eligible"] is False
