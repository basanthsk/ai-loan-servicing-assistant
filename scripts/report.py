"""Write docs/eval-report.md and docs/latency-report.md from evals/results/*.json.

Run: poetry run poe report
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "evals" / "results"


def load(name: str):
    p = RES / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def note(key: str) -> list[str]:
    """Run context written by whoever ran the evaluation (evals/results/notes.yaml)."""
    import yaml

    f = RES / "notes.yaml"
    text = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get(key) if f.exists() else None
    return [" ".join(text.split()), ""] if text else []


def pct(values, p):
    v = sorted(x for x in values if x is not None)
    return round(v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]) if v else None


def eval_report() -> str:
    lines = ["# Evaluation report", ""]
    total = passed = 0
    rows = []
    for pack in ("loans", "cards"):
        g = load(f"golden-{pack}.json")
        if not g:
            continue
        total += g["total"]
        passed += g["passed"]
        rows.append((pack, g))
    lines += [
        f"**Golden questions: {passed} of {total} passed.** Each question runs in a fresh session "
        "against the live LLM chain and the mock systems; checks are on exact figures (to the rupee), "
        "required words, forbidden claims and proposed actions. Figures were computed from the seeded "
        "data and calculators, not by an LLM.",
        "",
        *note("golden"),
    ]
    for pack, g in rows:
        lines += [
            f"## {pack.title()} ({g['passed']}/{g['total']}, run {g['run_at']})",
            "",
            "| Id | Question | Result | Total ms | Verified | Tools |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for r in g["results"]:
            res = "pass" if r["passed"] else "FAIL: " + "; ".join(r["problems"])
            lines.append(
                f"| {r['id']} | {r['question']} | {res} | {r.get('total_ms', '')} | "
                f"{'yes' if r.get('numbers_verified') else 'no'} | {', '.join(r.get('tools', []))} |"
            )
        lines.append("")

    b = load("decider-benchmark.json")
    if b:
        lines += [
            "## Decider benchmark",
            "",
            f"{b['results'][0]['cases']} labelled decisions from our journeys (intent routing across both packs, confirmations "
            "including negated and conditional replies, barge-in versus backchannel). Symmetry = same "
            "answer when the options are listed in reverse order.",
            "",
            *note("decider"),
            "| Backend | Answered | Accuracy (all) | Accuracy (answered) | Per kind | p50 ms | p95 ms | Symmetry |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for r in b["results"]:
            kinds = ", ".join(f"{k} {v}" for k, v in r["per_kind"].items())
            lines.append(
                f"| {r['backend']} | {r['answered']}/{r['cases']} | {r['accuracy_all']:.0%} | "
                f"{(r['accuracy_when_answered'] or 0):.0%} | {kinds} | {r['latency_ms_p50']} | "
                f"{r['latency_ms_p95']} | {r['symmetry'] or '-'} |"
            )
        lines.append("")

    rag = load("retrieval.json")
    if rag:
        m = rag["summary"]
        k = m["k"]
        lines += [
            "## Policy retrieval (RAG)",
            "",
            f"{m['answerable']} customer-style paraphrases of policy questions, each with the passage "
            f"that answers it, and {m['off_topic']} off-topic questions that should retrieve nothing "
            f"(`evals/retrieval.yaml`). Embeddings: {m['embedding_model']} (Hugging Face, run "
            f"locally); store: {m['vector_store']}; {m['passages']} passages; similarity cut-off "
            f"{m['min_score']}.",
            "",
            f"| Hit@1 | Recall@{k} | MRR | Off-topic rejected | Passages returned (avg) | Search p50 ms | p95 ms |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            f"| {m['hit_at_1']:.1%} | {m[f'recall_at_{k}']:.1%} | {m['mrr']} | "
            f"{m['off_topic_rejected']:.0%} | {m['avg_hits_returned']} | {m['search_ms_p50']} | "
            f"{m['search_ms_p95']} |",
            "",
            "Questions whose expected passage was not ranked first:",
            "",
        ]
        for c in rag["cases"]:
            if not c["ok"]:
                lines.append(
                    f"- {c['q']} (expected {c.get('expect', 'nothing')}, got {', '.join(c['got']) or 'nothing'})"
                )
        lines.append("")
    return "\n".join(lines)


def latency_report() -> str:
    lines = ["# Latency and performance report", ""]
    results = []
    for pack in ("loans", "cards"):
        g = load(f"golden-{pack}.json")
        if g:
            results += [r for r in g["results"] if r.get("total_ms")]
    if results:
        fast = [
            r
            for r in results
            if not any(t in ("next_emi", "card_minimum_due") for t in r.get("tools", []))
            and r.get("tools")
        ]
        fp = [
            r
            for r in results
            if any(t in ("next_emi", "card_minimum_due") for t in r.get("tools", []))
        ]
        lines += [
            "## Live turns (real LLM chain, free tiers)",
            "",
            f"From the golden evaluation: {len(results)} turns on the live LLM chain. The mock systems "
            "add 80-900 ms per call, as configured.",
            "",
            *note("live_turns"),
            "| Turn type | n | First status p50 / p95 ms | First words p50 / p95 ms | Total p50 / p95 ms |",
            "| --- | --- | --- | --- | --- |",
        ]
        for name, rs in (
            ("All turns", results),
            ("Fast path (no planner)", fp),
            ("Planner with tools", fast),
        ):
            if rs:
                lines.append(
                    f"| {name} | {len(rs)} | {pct([r.get('first_status_ms') for r in rs], 50)} / "
                    f"{pct([r.get('first_status_ms') for r in rs], 95)} | {pct([r.get('first_words_ms') for r in rs], 50)} / "
                    f"{pct([r.get('first_words_ms') for r in rs], 95)} | {pct([r['total_ms'] for r in rs], 50)} / "
                    f"{pct([r['total_ms'] for r in rs], 95)} |"
                )
        lines.append("")

    lt = load("loadtest.json")
    if lt:
        lines += [
            "## Load test",
            "",
            f"Concurrent customer conversations over the gateway WebSocket, LLM replaced by a stub ({lt['llm']}) "
            "so the test measures our system: gateway, orchestrator, Decider path, connectors, mock systems "
            "(with their injected latency) and the database. Each customer asks 3 questions with 1-4 s "
            "between them, a much faster pace than real callers.",
            "",
            "| Gateways | Concurrent | Turns | Errors | Turns/s | First status p50 / p95 | First words p50 / p95 | Turn p50 / p95 ms |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for r in lt["levels"]:
            lines.append(
                f"| {r.get('gateways', 1)} | {r['concurrent_conversations']} | {r['turns_completed']} | "
                f"{r['errors']} | {r['turns_per_s']} | {r['first_status_ms_p50']} / {r['first_status_ms_p95']} | "
                f"{r['first_words_ms_p50']} / {r['first_words_ms_p95']} | {r['turn_ms_p50']} / {r['turn_ms_p95']} |"
            )
        lines.append("")

    f = load("fault-injection.json")
    if f:
        lines += [
            "## Fault injection: Payments goes down mid-conversation",
            "",
            "| Turn | Phase | Payments call ms | Payments ok | Circuit | Failed fast | Turn ms |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for r in f["turns"]:
            lines.append(
                f"| {r['turn']} | {r['phase']} | {r['payments_call_ms']} | {r['payments_ok']} | "
                f"{r['circuit']} | {r['failed_fast']} | {r['turn_ms']} |"
            )
        lines.append("")

    v = load("voice.json")
    if v:
        lines += ["## Voice", "", v["summary"], "", "| Measurement | Value |", "| --- | --- |"] + [
            f"| {k} | {val} |" for k, val in v["measurements"].items()
        ]
        lines.append("")
    s = load("stt-wer.json")
    lines += ["## Speech-to-text accuracy (Indian-accented English)", ""]
    if s:
        lines.append(
            f"Word error rate {s['wer']:.1%} over {s['clips']} Svarah clips with Deepgram {s['model']}."
        )
    else:
        lines.append(
            "Not run yet: needs Hugging Face access (Svarah is gated). Run `poetry run python evals/stt_wer.py` on your machine."
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    (ROOT / "docs" / "eval-report.md").write_text(eval_report(), encoding="utf-8")
    (ROOT / "docs" / "latency-report.md").write_text(latency_report(), encoding="utf-8")
    print("Wrote docs/eval-report.md and docs/latency-report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
