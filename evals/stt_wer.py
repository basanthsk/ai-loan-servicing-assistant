"""Speech-to-text accuracy on Indian-accented English (Svarah), as word error rate.

Svarah (AI4Bharat, CC BY 4.0) is gated on Hugging Face: accept its terms on the
dataset page and put your token in HF_TOKEN. Run on your own machine:

    poetry install --with evals
    poetry run python evals/stt_wer.py --clips 50

Writes evals/results/stt-wer.json (read by the latency report).
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from core.settings import get_settings, require  # noqa: E402


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]", " ", text.lower()).split()


def wer(ref: str, hyp: str) -> tuple[int, int]:
    """Word-level edit distance and reference length."""
    r, h = words(ref), words(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)], len(r)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--clips", type=int, default=50)
    p.add_argument("--model", default="nova-3")
    args = p.parse_args()
    s = get_settings()
    require("hf_token", "deepgram_api_key", settings=s)

    import soundfile as sf  # installed with the `datasets` audio extra
    from datasets import Audio, load_dataset

    ds = load_dataset("ai4bharat/Svarah", split="test", streaming=True, token=s.hf_token)
    ds = ds.cast_column("audio", Audio(decode=True))
    errors = total = 0
    rows = []
    with httpx.Client(timeout=60) as client:
        for i, ex in enumerate(ds):
            if i >= args.clips:
                break
            buf = io.BytesIO()
            sf.write(buf, ex["audio"]["array"], ex["audio"]["sampling_rate"], format="WAV")
            t0 = time.perf_counter()
            r = client.post(
                "https://api.deepgram.com/v1/listen",
                params={"model": args.model, "smart_format": "false", "punctuate": "false"},
                headers={
                    "Authorization": f"Token {s.deepgram_api_key}",
                    "Content-Type": "audio/wav",
                },
                content=buf.getvalue(),
            )
            r.raise_for_status()
            hyp = r.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
            ref = ex.get("text") or ex.get("transcript") or ""
            e, n = wer(ref, hyp)
            errors, total = errors + e, total + n
            rows.append(
                {
                    "ref": ref,
                    "hyp": hyp,
                    "wer": round(e / max(n, 1), 3),
                    "ms": round((time.perf_counter() - t0) * 1000),
                }
            )
            print(f"{i + 1:3} WER {e / max(n, 1):.2f}  {ref[:60]!r}")
    result = {
        "dataset": "ai4bharat/Svarah (test)",
        "model": args.model,
        "clips": len(rows),
        "wer": round(errors / max(total, 1), 4),
        "run_at": time.strftime("%Y-%m-%d %H:%M %Z"),
        "samples": rows[:10],
    }
    out = ROOT / "evals" / "results" / "stt-wer.json"
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print(f"\nWord error rate over {len(rows)} clips: {result['wer']:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
