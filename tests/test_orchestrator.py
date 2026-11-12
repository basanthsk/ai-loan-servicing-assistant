"""Orchestrator behaviour with a scripted LLM and in-process mock systems (offline)."""

import pytest
from conftest import FakeChain, Recorder, text_reply, tool_reply

from core.session import Session


async def session_for(orch, customer="C1001"):
    s = Session(customer_id=customer)
    await orch.prefetch(session=s)
    return s


async def test_prefetch_loads_profile_and_loans(make_orchestrator):
    orch = make_orchestrator(FakeChain([]))
    s = await session_for(orch)
    assert s.customer_name == "Priya Sharma"
    assert {lo["loan_id"] for lo in s.accounts["loans"]["loans"]} == {"L-1042", "L-1043"}
    assert s.phone_last4 == "2345"


async def test_fast_path_answers_without_planner(make_orchestrator):
    chain = FakeChain([], decisions={"next EMI": "loan_next_emi"})
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    rec = Recorder()
    answer = await orch.handle(s, "What's my next EMI?", rec)
    assert "₹12,926" in answer and "L-1042" in answer
    assert rec.done["intent"] == "loan_next_emi"


async def test_planner_tool_call_and_verified_answer(make_orchestrator):
    chain = FakeChain(
        [
            tool_reply(("explain_emi_change", {"loan_id": "L-1042"})),
            text_reply(
                "Your EMI rose from ₹12,828 to ₹12,926 after the rate reset to 10.75% [LT-4.2]."
            ),
        ],
        decisions={"EMI": "loan_emi_change"},
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    rec = Recorder()
    await orch.handle(s, "Why did my EMI go up?", rec)
    assert rec.done["verified"] is True
    assert [t["name"] for t in rec.done["explain"]["tools"]] == ["explain_emi_change"]
    assert any(c["id"] == "LT-4.2" for c in rec.done["explain"]["clauses"])
    assert s.entities["loan_id"] == "L-1042"


async def test_invented_number_is_rewritten(make_orchestrator):
    chain = FakeChain(
        [
            tool_reply(("simulate_prepayment", {"loan_id": "L-1042", "amount": 50000})),
            text_reply("You would save ₹99,999 in interest."),
            text_reply("You would save ₹18,215 in interest if you keep the EMI."),
        ],
        decisions={"prepay": "loan_prepayment"},
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    rec = Recorder()
    answer = await orch.handle(s, "What if I prepay 50,000?", rec)
    assert rec.of("replace") and "18,215" in answer
    assert rec.done["verified"] is True


async def test_invalid_tool_arguments_are_returned_to_the_model(make_orchestrator):
    chain = FakeChain(
        [
            tool_reply(("simulate_prepayment", {"loan_id": "L-1042"})),  # missing amount
            text_reply("How much would you like to prepay?"),
        ],
        decisions={"prepay": "loan_prepayment"},
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    await orch.handle(s, "I want to prepay", Recorder())
    tool_msgs = [m for m in chain.planner_calls[-1] if m.get("role") == "tool"]
    assert "missing required argument 'amount'" in tool_msgs[0]["content"]


async def test_action_needs_confirmation_then_otp_and_is_audited(make_orchestrator, system_apps):
    from fastapi.testclient import TestClient

    chain = FakeChain(
        [
            tool_reply(
                (
                    "raise_payment_dispute",
                    {"payment_id": "P000019F", "reason": "funds were available"},
                )
            ),
            text_reply("I can raise that dispute. Shall I go ahead?"),
        ],
        decisions={"dispute": "loan_dispute"},
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    # Find the real June failed payment id for L-1042.
    payments = (
        TestClient(system_apps["payments"])
        .get("/customers/C1001/payments", params={"status": "failed"})
        .json()["payments"]
    )
    june = next(p for p in payments if p["due_date"] == "2026-06-05")
    chain.planner_script[0]["calls"] = (
        (
            "raise_payment_dispute",
            {"payment_id": june["payment_id"], "reason": "funds were available"},
        ),
    )

    rec = Recorder()
    await orch.handle(s, "Please dispute the June charge", rec)
    card = rec.of("action_required")[0]["action"]
    assert "5 June 2026" in card["summary"] and card["requires_otp"]
    assert s.pending_action is not None

    rec = Recorder()
    await orch.handle(s, "yes", rec)  # keyword decider: no LLM call
    otp = rec.of("otp_required")[0]["demo_code"]

    rec = Recorder()
    answer = await orch.handle(s, otp, rec)
    result = rec.of("action_result")[0]
    assert result["status"] == "executed" and result["result"]["request_id"].startswith("DSP-")
    assert result["result"]["request_id"] in answer
    assert s.pending_action is None

    audit = (
        TestClient(system_apps["serviceops"]).get("/audit", params={"customer_id": "C1001"}).json()
    )
    assert audit[0]["action"] == "raise_payment_dispute" and audit[0]["outcome"] == "created"


async def test_wrong_otp_is_refused(make_orchestrator):
    chain = FakeChain(
        [tool_reply(("request_foreclosure", {"loan_id": "L-1042"})), text_reply("Confirm?")],
        decisions={"close": "loan_foreclosure"},
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    await orch.handle(s, "I want to close my loan", Recorder())
    await orch.handle(s, "yes", Recorder())
    rec = Recorder()
    answer = await orch.handle(s, "000000" if s.otp_code != "000000" else "111111", rec)
    assert "didn't match" in answer and not rec.of("action_result")


async def test_no_cancels_pending_action(make_orchestrator):
    chain = FakeChain(
        [tool_reply(("send_repayment_statement", {"loan_id": "L-1042"})), text_reply("Confirm?")],
        decisions={"statement": "loan_statement"},
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    await orch.handle(s, "Send me a statement", Recorder())
    answer = await orch.handle(s, "no", Recorder())
    assert "cancelled" in answer and s.pending_action is None


async def test_unavailable_system_gives_partial_answer(make_orchestrator, system_apps):
    from services.common import Faults

    system_apps["payments"].state.faults = Faults(latency_ms=0, jitter_ms=0, down=True)
    chain = FakeChain(
        [
            tool_reply(("explain_failed_payments", {"months": 6})),
            text_reply("I can't reach the payments system right now."),
        ],
        decisions={"failed": "loan_payments"},
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    rec = Recorder()
    await orch.handle(s, "Which payments failed?", rec)
    tool_msgs = [m for m in chain.planner_calls[-1] if m.get("role") == "tool"]
    assert "unavailable" in tool_msgs[0]["content"]
    assert rec.of("tool")[0]["ok"] is False


async def test_greeting_discards_speculative_planner(make_orchestrator):
    chain = FakeChain(
        [text_reply("speculative text that must not be shown")], decisions={"hello": "greeting"}
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    rec = Recorder()
    answer = await orch.handle(s, "hello", rec)
    assert answer.startswith("Hi Priya")
    assert all("speculative" not in e.get("text", "") for e in rec.of("token"))


@pytest.mark.parametrize("text", ["yes", "Yes please", "go ahead.", "no", "cancel"])
def test_keyword_decider_handles_clear_replies(text):
    from core.decider import KeywordDecider

    assert KeywordDecider().decide("confirmation", text) is not None


def test_keyword_decider_defers_on_mixed_reply():
    from core.decider import KeywordDecider

    assert KeywordDecider().decide("confirmation", "yes but first tell me the charge") is None


async def test_voice_out_of_scope_asks_to_repeat(make_orchestrator):
    chain = FakeChain([], decisions={"painting": "out_of_scope"})
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    answer = await orch.handle(
        s, "Actually, check my last painting first.", Recorder(), channel="voice"
    )
    assert "say it again" in answer


async def test_provider_rejected_tool_call_is_retried(make_orchestrator):
    import httpx
    from openai import BadRequestError

    class RejectOnce(FakeChain):
        rejected = False

        async def astream(self, **kw):
            if not self.rejected:
                self.rejected = True
                req = httpx.Request("POST", "https://x/v1/chat/completions")
                raise BadRequestError(
                    "Tool call validation failed",
                    response=httpx.Response(400, request=req),
                    body=None,
                )
            return await super().astream(**kw)

    chain = RejectOnce([text_reply("Your rate is 10.75%.")], decisions={"rate": "loan_general"})
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    answer = await orch.handle(s, "What is my rate?", Recorder())
    assert "10.75" in answer


def test_optional_params_are_nullable_in_schemas():
    from core.registry import load_packs

    schema = load_packs()["loans"].tools["explain_failed_payments"].openai_schema()
    assert schema["function"]["parameters"]["properties"]["loan_id"]["type"] == ["string", "null"]


@pytest.mark.parametrize(
    "reply",
    ["Yes but first tell me the charge", "yes, wait, which loan?", "Yes, actually make it 30,000"],
)
async def test_conditional_yes_never_executes(make_orchestrator, reply):
    chain = FakeChain(
        [
            tool_reply(("send_repayment_statement", {"loan_id": "L-1042"})),
            text_reply("Confirm?"),
            text_reply("Sure, here's the detail first."),
        ],
        decisions={"Request: ": "yes", "statement": "loan_statement"},
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    await orch.handle(s, "Send me a statement", Recorder())
    rec = Recorder()
    await orch.handle(s, reply, rec)
    assert not [e for e in rec.of("action_result") if e["status"] == "executed"]


async def test_unusable_decider_output_degrades_to_planner(make_orchestrator):
    class BadJsonChain(FakeChain):
        async def achat(self, *, role, messages, **kw):
            r = await super().achat(role=role, messages=messages, **kw)
            if role == "fast":
                r.response.choices[0].message.content = "not json at all"
            return r

    chain = BadJsonChain([tool_reply(("next_emi", {})), text_reply("Your next EMI is ₹12,926.")])
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    rec = Recorder()
    answer = await orch.handle(s, "What's my next EMI?", rec)
    assert "12,926" in answer and rec.done["intent"] == "unrouted"


async def test_figures_without_a_tool_call_are_fetched_on_rewrite(make_orchestrator):
    chain = FakeChain(
        [
            text_reply("Paying ₹20,000 extra saves ₹9,670 in interest."),  # no tool call: invented
            tool_reply(("simulate_prepayment", {"loan_id": "L-1042", "amount": 20000})),
            text_reply("It saves ₹7,670 in interest and finishes 2 months earlier."),
        ],
        decisions={"extra": "loan_prepayment"},
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    rec = Recorder()
    answer = await orch.handle(s, "Would paying 20,000 extra reduce my tenure?", rec)
    assert "7,670" in answer and "9,670" not in answer and rec.done["verified"]


async def test_still_unverified_figures_are_never_shown(make_orchestrator):
    chain = FakeChain(
        [
            text_reply("You save ₹9,670."),
            text_reply("You save ₹9,670."),
            text_reply("You save ₹9,670."),
        ],
        decisions={"save": "loan_prepayment"},
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    rec = Recorder()
    answer = await orch.handle(s, "How much would I save?", rec)
    assert "9,670" not in answer and "won't guess" in answer and rec.done["verified"] is False


async def test_message_about_a_loan_and_a_card_gets_both_packs(make_orchestrator):
    chain = FakeChain(
        [
            tool_reply(("next_emi", {}), ("card_minimum_due", {})),
            text_reply(
                "Your next EMI is ₹6,787 on L-1043, due 20 November. "
                "On your card ending 4417 the minimum due is ₹2,154 by 20 November."
            ),
        ],
        decisions={"credit card": "loan_next_emi"},
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    rec = Recorder()
    answer = await orch.handle(s, "What's my next EMI and how much is due on my credit card?", rec)
    assert rec.done["pack"] == "loans+credit_cards"
    assert [t["name"] for t in rec.done["explain"]["tools"]] == ["next_emi", "card_minimum_due"]
    assert "₹2,154" in answer and rec.done["verified"] is True


async def test_single_product_message_stays_on_one_pack(make_orchestrator):
    chain = FakeChain([], decisions={"next EMI": "loan_next_emi"})
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    rec = Recorder()
    await orch.handle(s, "What's my next EMI?", rec)
    assert rec.done["pack"] == "loans"


async def test_empty_model_answer_is_replaced(make_orchestrator):
    chain = FakeChain([text_reply(""), text_reply("")], decisions={"why": "loan_emi_change"})
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    answer = await orch.handle(s, "why", Recorder())
    assert answer.startswith("Sorry, I couldn't put that answer together")


async def test_empty_answer_gets_one_write_up_retry(make_orchestrator):
    chain = FakeChain(
        [text_reply(""), text_reply("Your EMI rose because the benchmark rate went up [LT-4.2].")],
        decisions={"why": "loan_emi_change"},
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    answer = await orch.handle(s, "why", Recorder())
    assert answer.startswith("Your EMI rose"), answer
