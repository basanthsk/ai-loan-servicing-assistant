"""Retrieval evaluation for the policy knowledge base.

Runs every question in evals/retrieval.yaml against the real embedding model and
Chroma index and reports hit@1, recall@k, MRR, the share of off-topic questions
that correctly retrieve nothing, and search latency. Offline after the model is
downloaded; no LLM is involved.

    poetry run python evals/run_retrieval.py
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.knowledge import KnowledgeBase  # noqa: E402
from core.settings import get_settings  # noqa: E402

OUT = ROOT / "evals" / "results" / "retrieval.json"


def main() -> None:
    s = get_settings()
    kb = KnowledgeBase.from_settings(s)
    k = s.kb_top_k
    cases = yaml.safe_load((ROOT / "evals" / "retrieval.yaml").read_text(encoding="utf-8"))
    rows, times = [], []
    for pack, items in cases.items():
        for item in items:
            t0 = time.perf_counter()
            hits = kb.search(pack, item["q"], k)
            times.append((time.perf_counter() - t0) * 1000)
            ids = [h.id for h in hits]
            row = {
                "pack": pack,
                "q": item["q"],
                "got": ids,
                "top_score": hits[0].score if hits else None,
            }
            if item.get("none"):
                row.update(kind="none", ok=not ids)
            else:
                rank = ids.index(item["expect"]) + 1 if item["expect"] in ids else 0
                row.update(kind="answerable", expect=item["expect"], rank=rank, ok=rank == 1)
            rows.append(row)

    ans = [r for r in rows if r["kind"] == "answerable"]
    none = [r for r in rows if r["kind"] == "none"]
    summary = {
        "embedding_model": kb.model_name,
        "vector_store": "chroma (persistent, cosine)",
        "passages": len(kb.passages),
        "k": k,
        "min_score": kb.min_score,
        "answerable": len(ans),
        "hit_at_1": round(sum(r["rank"] == 1 for r in ans) / len(ans), 3),
        f"recall_at_{k}": round(sum(r["rank"] > 0 for r in ans) / len(ans), 3),
        "mrr": round(sum(1 / r["rank"] for r in ans if r["rank"]) / len(ans), 3),
        "off_topic": len(none),
        "off_topic_rejected": round(sum(r["ok"] for r in none) / len(none), 3),
        "avg_hits_returned": round(statistics.mean(len(r["got"]) for r in ans), 2),
        "search_ms_p50": round(statistics.median(times), 1),
        "search_ms_p95": round(sorted(times)[int(0.95 * (len(times) - 1))], 1),
        "index_build_ms": round(kb.index_ms),
    }
    OUT.write_text(json.dumps({"summary": summary, "cases": rows}, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    for r in rows:
        if not r["ok"]:
            print(
                "MISS",
                r["pack"],
                "|",
                r["q"],
                "| expected",
                r.get("expect", "nothing"),
                "| got",
                r["got"],
            )


if __name__ == "__main__":
    main()
