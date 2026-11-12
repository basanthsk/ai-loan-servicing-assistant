# Provider smoke test results

Generated 2026-11-12 05:23 UTC by `poetry run poe smoke-report`. Each LLM test checks the API is reachable, answers a chat prompt, makes a valid `get_loan` tool call, and measures streamed latency.

| Test | Provider and model | Where it ran | Result | Latency |
| --- | --- | --- | --- | --- |
| groq-planner | Groq / `openai/gpt-oss-120b` | cloud session | Pass | first token p50 408 ms, p95 618 ms |
| groq-fast | Groq / `openai/gpt-oss-20b` | cloud session | Pass | first token p50 406 ms, p95 437 ms |
| cerebras-planner | Cerebras / `gpt-oss-120b` | cloud session | Pass | first token p50 216 ms, p95 536 ms |
| openrouter | OpenRouter (free) / `nvidia/nemotron-3-ultra-550b-a55b:free` | cloud session | Pass | first token p50 1744 ms, p95 5622 ms |
| ollama-dev | – | – | Not run | Run on your machine: poetry run poe smoke-ollama |
| ollama-dev-fast | – | – | Not run | Run on your machine: poetry run poe smoke-ollama |
| fallback | Fallback chain / `fake-429 -> groq -> cerebras -> openrouter` | cloud session | Pass | – |
| deepgram-tts | Deepgram TTS / `aura-2-thalia-en` | your machine | Pass | first audio byte p50 371 ms, p95 944 ms |
| deepgram-stt | Deepgram STT / `nova-3` | your machine | Pass | 676 ms |
| decider | Decider / `llm_json via groq,cerebras,openrouter (fast role)` | cloud session | Pass | 298–457 ms per decision |

## Details

### groq-planner

- **PASS** reachable (508 ms): 11 models listed; target found
- **PASS** chat (543 ms): reply='OK'
- **PASS** tool call (531 ms): get_loan({"loan_id":"L-1042"})
- **PASS** latency: time to first token p50 408 ms, p95 618 ms; total p50 476 ms (5/5 runs ok)

### groq-fast

- **PASS** reachable (163 ms): 11 models listed; target found
- **PASS** chat (324 ms): reply='OK'
- **PASS** tool call (527 ms): get_loan({"loan_id":"L-1042"})
- **PASS** latency: time to first token p50 406 ms, p95 437 ms; total p50 410 ms (5/5 runs ok)

### cerebras-planner

- **PASS** reachable (356 ms): 2 models listed; target found
- **PASS** chat (234 ms): reply='OK'
- **PASS** tool call (272 ms): get_loan({"loan_id":"L-1042"})
- **PASS** latency: time to first token p50 216 ms, p95 536 ms; total p50 218 ms (5/5 runs ok)
- Note: rate limited 1 time(s) during latency runs (free-tier limit)

### openrouter

- **PASS** reachable (539 ms): 433 models listed; target found
- **PASS** chat (1004 ms): reply='OK'
- **PASS** tool call (1156 ms): get_loan({"loan_id":"L-1042"})
- **PASS** latency: time to first token p50 1744 ms, p95 5622 ms; total p50 2344 ms (3/3 runs ok)
- Note: Chosen from 1 free tool-capable models; set OPENROUTER_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free

### fallback

- **PASS** fails over on 429: answered by groq; attempts: fake-429 RateLimitError (25 ms), groq ok (409 ms)

### deepgram-tts

- **PASS** synthesise mp3 (3534 ms): 36,288 bytes saved to output/tts_sample.mp3
- **PASS** latency: time to first audio byte p50 371 ms, p95 944 ms

### deepgram-stt

- **PASS** file transcription (676 ms): 'Your next EMI of INR 12,450 is due on the December 5.'
- **PASS** streaming transcription: 5 partial results, first after 1236 ms of streamed audio; final='Your next EMI of 12,450 rupees is due on the December 5.'

### decider

- **PASS** intent (298 ms): "What's my next EMI?" -> next_emi (next_emi 0.90, payment_history 0.05, prepayment_quote 0.02, foreclosure 0.02, out_of_scope 0.01) via groq
- **PASS** barge-in (407 ms): 'mm-hmm' -> backchannel (backchannel 0.90, interruption 0.10) via groq
- **PASS** confirmation (457 ms): "No wait, don't do that yet." -> no (no 0.90, yes 0.05, changed_mind 0.03, unclear 0.02) via groq
- Note: Baseline only: the LLM backend is a fallback. Decision models are benchmarked against it in evals/bench_decider.py.
