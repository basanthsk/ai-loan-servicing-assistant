# Latency and performance report

## Live turns (real LLM chain, free tiers)

From the golden evaluation: 69 turns on the live LLM chain. The mock systems add 80-900 ms per call, as configured.

Same run as the golden questions: Qwen 3.8 27B on Cerebras for every call, no rate-limit waits. The mock systems' injected latency (80-900 ms per call) is included.

| Turn type | n | First status p50 / p95 ms | First words p50 / p95 ms | Total p50 / p95 ms |
| --- | --- | --- | --- | --- |
| All turns | 69 | 520 / 1426 | 542 / 1409 | 1090 / 2611 |
| Fast path (no planner) | 10 | 331 / 1006 | 354 / 948 | 354 / 1424 |
| Planner with tools | 53 | 596 / 1426 | 555 / 1396 | 1189 / 2611 |

## Load test

Concurrent customer conversations over the gateway WebSocket, LLM replaced by a stub (stub: decider 300 ms, planner first token 450 ms) so the test measures our system: gateway, orchestrator, Decider path, connectors, mock systems (with their injected latency) and the database. Each customer asks 3 questions with 1-4 s between them, a much faster pace than real callers.

| Gateways | Concurrent | Turns | Errors | Turns/s | First status p50 / p95 | First words p50 / p95 | Turn p50 / p95 ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 10 | 30 | 0 | 1.4 | 458 / 467 | 1164 / 1790 | 1365 / 1990 |
| 1 | 50 | 150 | 0 | 6.3 | 457 / 462 | 1108 / 1524 | 1308 / 1729 |
| 1 | 100 | 300 | 0 | 12.8 | 458 / 481 | 1130 / 1357 | 1330 / 1558 |
| 1 | 200 | 600 | 0 | 24.7 | 464 / 515 | 1138 / 1380 | 1338 / 1583 |
| 1 | 400 | 1200 | 0 | 35.0 | 476 / 649 | 1172 / 1620 | 1371 / 1835 |
| 1 | 700 | 2100 | 0 | 48.3 | 698 / 1358 | 1673 / 5562 | 1895 / 5782 |
| 1 | 1000 | 3000 | 0 | 47.7 | 770 / 1883 | 2063 / 8382 | 2289 / 8725 |
| 3 | 1000 | 3000 | 0 | 77.2 | 476 / 592 | 1160 / 1445 | 1365 / 1663 |

## Fault injection: Payments goes down mid-conversation

| Turn | Phase | Payments call ms | Payments ok | Circuit | Failed fast | Turn ms |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | normal | 555 | True | closed | False | 2003 |
| 2 | normal | 476 | True | closed | False | 1848 |
| 3 | payments down | 1721 | False | closed | False | 2834 |
| 4 | payments down | 1947 | False | open | False | 3061 |
| 5 | payments down | 0 | False | open | True | 1113 |
| 6 | payments down | 0 | False | open | True | 1119 |
| 7 | payments down | 0 | False | open | True | 1119 |
| 8 | payments down | 0 | False | open | True | 1118 |
| 9 | recovered | 920 | True | closed | False | 2228 |
| 10 | recovered | 626 | True | closed | False | 2028 |

## Voice

Sandbox rows: measured in the cloud sandbox with real WebRTC calls from headless Chromium using a fake microphone (synthesised customer speech). The sandbox blocks WebSockets, so those runs used the HTTP fallbacks: Groq Whisper (speech-to-text after each pause) and Deepgram HTTP text-to-speech. Local rows: Deepgram smoke tests on the owner's machine in India on 12 November 2026, where streaming works; Deepgram's servers are in the US, so each request costs about 200 ms more than from the sandbox. End-of-speech to first audio with a real microphone is still to be measured (evals/results/voice-calls.jsonl).

| Measurement | Value |
| --- | --- |
| End of speech to first audio, fast path ('What is my next EMI?'), sandbox | 2.5 s |
| End of speech to first audio, planner + tools ('Why did my EMI go up?'), sandbox | 3.1 - 3.6 s |
| Filler line when a slow system is called, sandbox | spoken, about 2.6 s after end of speech |
| Barge-in: assistant stops after the customer starts talking over it, sandbox | about 1.2 s (0.8 s sustained-speech rule + VAD) |
| Backchannel ('mm-hmm') while the assistant talks | ignored, no interruption (unit tested) |
| Speech-to-text streaming, local (Deepgram Nova-3) | works; 5 partial results while speaking, final transcript correct ('Your next EMI of 12,450 rupees is due on the December 5.') |
| Speech-to-text on a file, local / sandbox | 676 ms / 161 ms |
| Text-to-speech first audio byte, local (Deepgram Aura-2, 5 runs) | p50 371 ms, p95 944 ms (sandbox p50 179 ms) |

## Speech-to-text accuracy (Indian-accented English)

Not run yet: needs Hugging Face access (Svarah is gated). Run `poetry run python evals/stt_wer.py` on your machine.
