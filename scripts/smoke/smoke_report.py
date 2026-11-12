"""Build scripts/smoke/results/summary.md from scripts/smoke/results/*.json.

Run: poetry run poe smoke-report
"""

import json
import time

from common import RESULTS_DIR, ROOT

ORDER = [
    "groq-planner",
    "groq-fast",
    "cerebras-planner",
    "openrouter",
    "ollama-dev",
    "ollama-dev-fast",
    "fallback",
    "deepgram-tts",
    "deepgram-stt",
    "decider",
]
EXPECTED = {
    "ollama-dev": "Run on your machine: poetry run poe smoke-ollama",
    "ollama-dev-fast": "Run on your machine: poetry run poe smoke-ollama",
}


def status(r: dict) -> str:
    checks = r["checks"]
    if any(c.get("skipped") for c in checks):
        return "Pass, one check to run on your machine" if r["ok"] else "Fail"
    return "Pass" if r["ok"] else "Fail"


def latency(r: dict) -> str:
    lat = r.get("latency") or {}
    if "ttft_ms_p50" in lat:
        return f"first token p50 {lat['ttft_ms_p50']} ms, p95 {lat['ttft_ms_p95']} ms"
    if "first_audio_byte_ms_p50" in lat:
        return f"first audio byte p50 {lat['first_audio_byte_ms_p50']} ms, p95 {lat['first_audio_byte_ms_p95']} ms"
    if "ms_min" in lat:
        return f"{lat['ms_min']}–{lat['ms_max']} ms per decision"
    ms = [c["ms"] for c in r["checks"] if c.get("ms")]
    return f"{round(min(ms))} ms" if ms else "–"


def main() -> int:
    results = {p.stem: json.loads(p.read_text()) for p in RESULTS_DIR.glob("*.json")}
    lines = [
        "# Provider smoke test results",
        "",
        f"Generated {time.strftime('%Y-%m-%d %H:%M %Z')} by `poetry run poe smoke-report`. "
        "Each LLM test checks the API is reachable, answers a chat prompt, makes a valid "
        "`get_loan` tool call, and measures streamed latency.",
        "",
        "| Test | Provider and model | Where it ran | Result | Latency |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name in ORDER:
        r = results.get(name)
        if r is None:
            note = EXPECTED.get(name, "Not run yet")
            lines.append(f"| {name} | – | – | Not run | {note} |")
            continue
        lines.append(
            f"| {name} | {r['provider']} / `{r['model']}` | {r.get('run_where', '')} | {status(r)} | {latency(r)} |"
        )

    lines += ["", "## Details", ""]
    for name in ORDER:
        r = results.get(name)
        if r is None:
            continue
        lines.append(f"### {name}")
        lines.append("")
        for c in r["checks"]:
            mark = "SKIP" if c.get("skipped") else ("PASS" if c["ok"] else "FAIL")
            timing = f" ({c['ms']:.0f} ms)" if c.get("ms") else ""
            lines.append(f"- **{mark}** {c['name']}{timing}: {c['detail']}")
        for note in r.get("notes", []):
            lines.append(f"- Note: {note}")
        lines.append("")

    out = ROOT / "scripts" / "smoke" / "results" / "summary.md"
    out.write_text("\n".join(lines))
    print(f"Wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
