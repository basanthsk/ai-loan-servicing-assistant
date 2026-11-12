"""Policy knowledge base (RAG): chunking, indexing, search, the Policy KB endpoint and
the search_policy tool. Uses the offline hash embedder (KB_EMBEDDER=hash in conftest);
the real Hugging Face model is tested only when it has already been downloaded."""

from pathlib import Path

import pytest
from conftest import FakeChain, Recorder, text_reply, tool_reply
from fastapi.testclient import TestClient
from test_orchestrator import session_for

from core.knowledge import (
    KnowledgeBase,
    hash_embedder,
    load_markdown,
    load_passages,
)
from core.settings import Settings
from services import policy
from services.common import Faults


@pytest.fixture(scope="module")
def kb(tmp_path_factory):
    return KnowledgeBase(
        hash_embedder(), "hash-512", tmp_path_factory.mktemp("chroma"), min_score=0.2
    )


def test_every_passage_has_an_id_pack_and_text():
    ps = load_passages()
    assert {p.pack for p in ps} == {"loans", "credit_cards"}
    assert all(p.id and p.title and len(p.text) > 40 for p in ps)
    ids = {p.id for p in ps}
    assert {"LT-6.1", "KB-L-01", "KB-L-11", "CC-5.1", "KB-C-03"} <= ids


def test_markdown_sections_keep_their_own_source(tmp_path):
    f = tmp_path / "x.md"
    f.write_text(
        "---\ntitle: T\npack: loans\nsource: default\n---\n"
        "## [KB-X-01] First\nBody one.\nSource: RBI circular\non two lines.\n\n"
        "## [KB-X-02] Second\nBody two.\n"
    )
    a, b = load_markdown(f)
    assert (a.id, a.text, a.source) == ("KB-X-01", "Body one.", "RBI circular on two lines.")
    assert (b.id, b.source) == ("KB-X-02", "default")


def test_markdown_without_pack_is_rejected(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("---\ntitle: T\n---\n## [KB-X-01] A\nB\n")
    with pytest.raises(ValueError, match="pack"):
        load_markdown(f)


def test_search_finds_the_right_section(kb):
    assert kb.search("loans", "how do I change my EMI due date")[0].id == "KB-L-01"
    assert kb.search("credit_cards", "redeem reward points for vouchers")[0].id == "KB-C-06"


def test_search_stays_inside_the_pack(kb):
    hits = kb.search("loans", "reward points on my credit card billing cycle")
    assert all(not h.id.startswith(("CC-", "KB-C")) for h in hits)


def test_clause_id_in_question_is_returned_first(kb):
    hits = kb.search("loans", "what does LT-6.3 say?")
    assert hits[0].id == "LT-6.3" and hits[0].score == 1.0


def test_unrelated_question_returns_nothing(kb):
    assert kb.search("loans", "zebra giraffe volcano") == []


def test_index_is_reused_and_rebuilt_when_documents_change(tmp_path):
    ps = load_passages()
    KnowledgeBase(hash_embedder(), "hash-512", tmp_path, ps)
    again = KnowledgeBase(hash_embedder(), "hash-512", tmp_path, ps)
    assert again.collections["loans"].count() == len([p for p in ps if p.pack == "loans"])
    fewer = [p for p in ps if p.id != "KB-L-01"]
    rebuilt = KnowledgeBase(hash_embedder(), "hash-512", tmp_path, fewer)
    assert "KB-L-01" not in rebuilt.collections["loans"].get()["ids"]


def test_policy_search_endpoint():
    saved = policy.app.state.faults
    policy.app.state.faults = Faults(latency_ms=0, jitter_ms=0, failure_rate=0)
    try:
        c = TestClient(policy.app)
        r = c.get("/search/loans", params={"query": "change the EMI date"}).json()
        assert r["results"][0]["id"] == "KB-L-01"
        assert {"id", "title", "text", "source", "score"} <= set(r["results"][0])
        assert c.get("/search/nope", params={"query": "x y"}).status_code == 404
    finally:
        policy.app.state.faults = saved


async def test_search_policy_tool_cites_passages(make_orchestrator):
    chain = FakeChain(
        [
            tool_reply(("search_policy", {"query": "How can I change my EMI date?"})),
            text_reply(
                "You can move the EMI date once a year, to the 5th, 10th or 15th [KB-L-01]."
            ),
        ],
        decisions={"EMI date": "loan_policy"},
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    rec = Recorder()
    answer = await orch.handle(s, "How can I change my EMI date?", rec)
    assert "[KB-L-01]" in answer
    assert rec.done["verified"] is True  # 5th, 10th, 15th are in the passage
    assert any(c["id"] == "KB-L-01" for c in rec.done["explain"]["clauses"])


@pytest.mark.skipif(
    not (
        Path(Settings(_env_file=None).kb_model_dir) / "models--Qdrant--bge-small-en-v1.5-onnx-Q"
    ).exists(),
    reason="embedding model not downloaded (run poe index once)",
)
def test_real_embedding_model_ranks_paraphrases(tmp_path):
    s = Settings(_env_file=None, kb_embedder="fastembed", kb_chroma_dir=str(tmp_path))
    kb = KnowledgeBase.from_settings(s)
    assert kb.search("loans", "who can I escalate to if my complaint is ignored")[0].id == "KB-L-11"
    assert kb.search("credit_cards", "bank raised my limit without asking me")[0].id == "KB-C-04"
    assert kb.search("loans", "what is the capital of France") == []


async def test_out_of_scope_policy_question_is_answered_from_the_documents(make_orchestrator):
    chain = FakeChain(
        [
            tool_reply(("search_policy", {"query": "complaint not resolved, escalate"})),
            text_reply("After 30 days you can escalate to the RBI Ombudsman [KB-L-11]."),
        ],
        decisions={"complaint": "out_of_scope"},
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    rec = Recorder()
    answer = await orch.handle(
        s, "My complaint is not resolved, how do I escalate to the ombudsman?", rec
    )
    assert "[KB-L-11]" in answer
    assert rec.done["pack"] == "loans"


async def test_real_out_of_scope_message_is_still_refused(make_orchestrator):
    chain = FakeChain([], decisions={"zebra": "out_of_scope"})
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    answer = await orch.handle(s, "zebra giraffe volcano", Recorder())
    assert "only help" in answer


async def test_uncited_policy_answer_gets_the_best_passage_cited(make_orchestrator):
    chain = FakeChain(
        [
            tool_reply(("search_policy", {"query": "change the EMI date"})),
            text_reply("You can move it once a year, to the 5th, 10th or 15th."),
        ],
        decisions={"EMI date": "loan_policy"},
    )
    orch = make_orchestrator(chain)
    s = await session_for(orch)
    rec = Recorder()
    answer = await orch.handle(s, "Can I change my EMI date?", rec)
    assert answer.endswith("[KB-L-01]")
