"""Mock Policy Knowledge system: product terms, prepayment rules, charges, eligibility.

Serves clause text by id, and semantic search (RAG) over the clauses and the
FAQ documents in data/kb/ through core.knowledge. The rules themselves live in
each domain pack's rules.yaml and are evaluated by the platform's rules engine;
this system is the source of the clause wording the assistant cites.
"""

import asyncio
import time
from contextlib import asynccontextmanager

import yaml
from fastapi import HTTPException, Query

from core.knowledge import get_knowledge_base
from core.settings import get_settings
from services.common import ROOT, create_app


@asynccontextmanager
async def lifespan(app):
    # Load the embedding model and open (or build) the index in the background, so
    # the first question does not wait for it.
    asyncio.get_running_loop().run_in_executor(None, get_knowledge_base)
    yield


app = create_app("policy", lifespan)


def load_clauses() -> dict[str, dict]:
    clauses = {}
    for f in sorted((ROOT / "data" / "policy").glob("*.yaml")):
        for c in yaml.safe_load(f.read_text(encoding="utf-8"))["clauses"]:
            clauses[c["id"]] = {**c, "document": f.stem}
    return clauses


CLAUSES = load_clauses()


@app.get("/clauses")
def list_clauses(document: str | None = None):
    return [
        {"id": c["id"], "title": c["title"], "document": c["document"]}
        for c in CLAUSES.values()
        if document is None or c["document"] == document
    ]


@app.get("/clauses/{clause_id}")
def get_clause(clause_id: str):
    if clause_id not in CLAUSES:
        raise HTTPException(404, "clause not found")
    return CLAUSES[clause_id]


@app.get("/search")
async def search_all(query: str = Query(min_length=2, max_length=500)):
    """Best passage in each pack's documents. The assistant uses it to check whether a
    message the router thought out of scope is in fact a policy question."""
    kb = await asyncio.to_thread(get_knowledge_base)
    best = []
    for pack in kb.packs():
        hits = await asyncio.to_thread(kb.search, pack, query, 1)
        if hits:
            best.append({"pack": pack, **hits[0].as_dict()})
    best.sort(key=lambda h: -h["score"])
    return {"query": query, "results": best}


@app.get("/search/{pack}")
async def search(pack: str, query: str = Query(min_length=2, max_length=500), k: int = 0):
    """Passages most relevant to a question, from one pack's policy documents."""
    kb = await asyncio.to_thread(get_knowledge_base)  # built on first call
    k = min(k or get_settings().kb_top_k, 8)
    t0 = time.perf_counter()
    try:
        hits = await asyncio.to_thread(kb.search, pack, query, k)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    return {
        "query": query,
        "results": [h.as_dict() for h in hits],
        "note": "" if hits else "nothing relevant found in the policy documents",
        "search_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
