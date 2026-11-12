"""Tasks 3.6 and 3.7: Deepgram text-to-speech (Aura) and speech-to-text (Nova).

The test makes its own audio: it synthesises a loan sentence with Aura, then
transcribes that audio back, once as a file upload and once over the streaming
WebSocket that the voice pipeline will use.

Run: poetry run poe smoke-deepgram
"""

import asyncio
import json
import statistics
import time

import httpx
import websockets

from common import ROOT, CheckResult, SmokeResult, pct
from core.settings import get_settings, require

API = "https://api.deepgram.com/v1"
WSS = "wss://api.deepgram.com/v1/listen"
TTS_MODEL = "aura-2-thalia-en"
STT_MODEL = "nova-3"
SENTENCE = "Your next EMI of twelve thousand four hundred and fifty rupees is due on the fifth of December."
EXPECT = ["emi", "december"]
OUT_DIR = ROOT / "scripts" / "smoke" / "output"


def tts(client: httpx.Client, key: str, params: dict) -> tuple[bytes, float, float]:
    """Return audio bytes, time to first byte (ms) and total time (ms)."""
    t0 = time.perf_counter()
    first = None
    chunks = []
    with client.stream(
        "POST",
        f"{API}/speak",
        params={"model": TTS_MODEL, **params},
        headers={"Authorization": f"Token {key}", "Content-Type": "application/json"},
        json={"text": SENTENCE},
    ) as r:
        r.raise_for_status()
        for chunk in r.iter_bytes():
            if first is None and chunk:
                first = time.perf_counter()
            chunks.append(chunk)
    end = time.perf_counter()
    return b"".join(chunks), ((first or end) - t0) * 1000, (end - t0) * 1000


async def stream_stt(key: str, pcm: bytes, sample_rate: int) -> tuple[list[str], str, float | None]:
    """Send raw PCM in real-time-sized chunks; return interim texts, final text, first-result ms."""
    url = (
        f"{WSS}?model={STT_MODEL}&encoding=linear16&sample_rate={sample_rate}"
        "&channels=1&interim_results=true&smart_format=true"
    )
    interim, final_parts = [], []
    first_ms = None
    async with websockets.connect(url, additional_headers={"Authorization": f"Token {key}"}) as ws:
        t0 = time.perf_counter()

        async def sender():
            step = sample_rate * 2 // 10  # 100 ms of 16-bit mono audio
            for i in range(0, len(pcm), step):
                await ws.send(pcm[i : i + step])
                await asyncio.sleep(0.1)
            await ws.send(json.dumps({"type": "CloseStream"}))

        async def receiver():
            nonlocal first_ms
            async for msg in ws:
                data = json.loads(msg)
                if data.get("type") != "Results":
                    continue
                text = data["channel"]["alternatives"][0]["transcript"]
                if text and first_ms is None:
                    first_ms = (time.perf_counter() - t0) * 1000
                if text:
                    (final_parts if data.get("is_final") else interim).append(text)

        await asyncio.gather(sender(), receiver())
    return interim, " ".join(final_parts), first_ms


def main() -> int:
    s = get_settings()
    require("deepgram_api_key", settings=s)
    key = s.deepgram_api_key
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(timeout=60)

    # --- Text-to-speech (task 3.7) ---
    tts_result = SmokeResult(provider="Deepgram TTS", model=TTS_MODEL)
    print(f"\n== Deepgram text-to-speech / {TTS_MODEL}")
    try:
        mp3, ttfb, total = tts(client, key, {})
        (OUT_DIR / "tts_sample.mp3").write_bytes(mp3)
        tts_result.add(
            CheckResult(
                "synthesise mp3",
                len(mp3) > 1000,
                f"{len(mp3):,} bytes saved to output/tts_sample.mp3",
                total,
            )
        )
        ttfbs = [ttfb]
        for _ in range(4):
            _, t, _ = tts(client, key, {})
            ttfbs.append(t)
        tts_result.latency = {
            "runs": len(ttfbs),
            "first_audio_byte_ms_p50": round(statistics.median(ttfbs)),
            "first_audio_byte_ms_p95": round(pct(ttfbs, 95)),
        }
        tts_result.add(
            CheckResult(
                "latency",
                True,
                f"time to first audio byte p50 {tts_result.latency['first_audio_byte_ms_p50']} ms, "
                f"p95 {tts_result.latency['first_audio_byte_ms_p95']} ms",
            )
        )
    except Exception as e:  # noqa: BLE001
        tts_result.add(CheckResult("synthesise mp3", False, f"{type(e).__name__}: {e}"))
    tts_result.save("deepgram-tts")

    # --- Speech-to-text (task 3.6) ---
    stt_result = SmokeResult(provider="Deepgram STT", model=STT_MODEL)
    print(f"\n== Deepgram speech-to-text / {STT_MODEL}")
    try:
        t0 = time.perf_counter()
        r = client.post(
            f"{API}/listen",
            params={"model": STT_MODEL, "smart_format": "true"},
            headers={"Authorization": f"Token {key}", "Content-Type": "audio/mpeg"},
            content=(OUT_DIR / "tts_sample.mp3").read_bytes(),
        )
        r.raise_for_status()
        text = r.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
        ok = all(w in text.lower() for w in EXPECT)
        stt_result.add(
            CheckResult("file transcription", ok, f"{text!r}", (time.perf_counter() - t0) * 1000)
        )
    except Exception as e:  # noqa: BLE001
        stt_result.add(CheckResult("file transcription", False, f"{type(e).__name__}: {e}"))

    try:
        rate = 16000
        pcm, _, _ = tts(
            client, key, {"encoding": "linear16", "sample_rate": rate, "container": "none"}
        )
        interim, final, first_ms = asyncio.run(stream_stt(key, pcm, rate))
        ok = bool(interim) and all(w in final.lower() for w in EXPECT)
        stt_result.latency = {"first_partial_result_ms": round(first_ms) if first_ms else None}
        stt_result.add(
            CheckResult(
                "streaming transcription",
                ok,
                f"{len(interim)} partial results, first after {first_ms:.0f} ms of streamed audio; final={final!r}"
                if first_ms
                else "no results",
            )
        )
    except websockets.InvalidStatus as e:
        body = bytes(e.response.body or b"").decode(errors="replace")
        if "did not include 'upgrade'" in body:
            # A proxy in front of this machine strips the WebSocket upgrade header.
            stt_result.add(
                CheckResult(
                    "streaming transcription",
                    False,
                    "skipped: a network proxy here blocks WebSocket upgrades; run this test on your machine",
                    skipped=True,
                )
            )
            stt_result.notes.append(
                "Streaming speech-to-text must be verified on your machine (proxy blocks WebSockets)"
            )
        else:
            stt_result.add(
                CheckResult(
                    "streaming transcription", False, f"HTTP {e.response.status_code}: {body[:200]}"
                )
            )
    except Exception as e:  # noqa: BLE001
        stt_result.add(CheckResult("streaming transcription", False, f"{type(e).__name__}: {e}"))
    stt_result.save("deepgram-stt")

    return 0 if tts_result.ok and stt_result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
