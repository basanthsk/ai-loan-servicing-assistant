# AI Loan Servicing Assistant

A working proof of concept of an AI-powered loan servicing experience, by text and
live voice. Customers get answers specific to their own accounts, with every figure
computed by code and every rule cited, and complete service requests (disputes,
prepayment and foreclosure requests, statements) across simulated enterprise
systems. Credit-card servicing is the second workflow, added as a domain pack on the
same platform without changing platform code.

| Read | For |
| --- | --- |
| [docs/WRITEUP.md](docs/WRITEUP.md) | Short technical write-up: architecture, key decisions and trade-offs, headline measurements |
| [docs/architecture.md](docs/architecture.md) | Architecture, engineering decisions, trade-offs, limitations |
| [docs/eval-report.md](docs/eval-report.md) | Golden-question results and Decider benchmark |
| [docs/latency-report.md](docs/latency-report.md) | Live latency, load test to 1,000 conversations, fault injection, voice |
| [docs/demo-script.md](docs/demo-script.md) | A 14-minute demo walkthrough |

## What it does

- **Precise answers:** "My EMI went up, why, and what if I prepay ₹50,000?" is answered
  from loan data, the rate-reset rule and the prepayment calculator, with clauses cited
  and a "How I got this" panel. Figures that can't be verified are never shown.
- **Voice:** talk to it in the browser; interrupt it, change topic, follow up. It speaks
  a short filler while slow systems answer.
- **Actions:** disputes, prepayment and foreclosure requests, statements, card blocks,
  each with confirmation, OTP step-up where needed, idempotency and an audit log.
- **Resilience:** six mock systems with injected latency and failures; retries, circuit
  breakers, caching and partial answers. Switch a system off from the web app to see it.
- **Policy questions (RAG):** "How do I change my EMI date?" or "Am I liable for a
  fraudulent card charge?" are answered from policy documents retrieved by semantic
  search (Hugging Face `BAAI/bge-small-en-v1.5` embeddings run locally, Chroma vector
  store), with the passage cited. No key or paid service is involved.
- **Platform:** domain packs (`packs/loans`, `packs/credit_cards`) on a shared core.

## Run it on your machine

You need Python 3.12, [Poetry](https://python-poetry.org/) 2.x and the API keys below.
No Node or Docker is needed.

1. Install dependencies (the virtual environment is created in `.venv`):

```bash
poetry install
```

2. Create your `.env` from the template and fill in the keys (it is git-ignored):

```bash
cp .env.example .env
```

On Windows PowerShell use `copy .env.example .env`. Keys: `CEREBRAS_API_KEY` (primary LLM),
`GROQ_API_KEY` (fallback LLM), `DEEPGRAM_API_KEY` (voice) and `OPENROUTER_API_KEY`
(fallback, optional). `OPENAI_API_KEY` and `TYPESAFE_API_KEY` are only for the
optional paid OpenAI provider and the JEV Decider. Check which are set without
printing them:

```bash
poetry run poe check-env
```

3. Create the demo database and the policy search
   index (downloads the 65 MB embedding model from Hugging Face once; no token needed):

```bash
poetry run poe seed
```

The database is deterministic. It holds three hand-built demo customers and 47
generated customers. The generated customers' repayment and card behaviour replays
real, anonymised histories from the open Home Credit dataset (see
[data/open/README.md](data/open/README.md)).

```bash
poetry run poe index
```

4. Start the mock systems and the gateway:

```bash
poetry run poe up
```

5. Open http://127.0.0.1:8000, log in as **Priya Sharma**, and try the suggestions or
   the [demo script](docs/demo-script.md). Click the microphone for voice (Chrome or
   Edge; allow the microphone).

No keys? The assistant falls back to a local Ollama model (`ollama pull qwen3:8b`).

## Commands

| Command | What it does |
| --- | --- |
| `poetry run poe up` | Start the six mock systems and the gateway (`--services` for systems only) |
| `poetry run poe seed` | Recreate `data/bank.db` |
| `poetry run poe chat` | Chat in the terminal (add `--voice` for spoken-style answers, `--trace` for timings) |
| `poetry run poe test` | 100+ offline tests (scripted LLM, in-process systems, no keys needed) |
| `poetry run poe lint` / `fmt` | Ruff |
| `poetry run poe smoke-all` | Provider smoke tests (results in `scripts/smoke/results/`) |
| `poetry run poe smoke-ollama` | Local model smoke test |
| `poetry run poe index` | Download the embedding model and build the policy search index (also built on first use) |
| `poetry run poe eval-rag` | Retrieval accuracy of the policy search (hit@1, recall@4, off-topic rejection) |
| `poetry run poe eval` | Golden questions against the live LLM chain (needs `poe up --services`) |
| `poetry run poe report` | Rebuild the evaluation and latency reports from `evals/results/` |
| `poetry run python evals/bench_decider.py` | Decider benchmark (`--backends keyword+llm:cerebras-qwen,keyword+jev`) |
| `poetry run python evals/build_decision_set.py --with-open-data` | Rebuild the decision set with real phrasings from BANKING77 and Bitext (`poetry install --with evals`) |
| `poetry run python scripts/llm_stub.py` + `poetry run poe loadtest` | Load test against an LLM stub (see latency report) |
| `poetry run python scripts/fault_demo.py` | Payments outage and recovery |
| `poetry run python evals/stt_wer.py` | Speech-to-text accuracy on Svarah (needs `HF_TOKEN`, `poetry install --with evals`) |

## Results of a local test session

| File | What it holds | Written by |
| --- | --- | --- |
| `evals/results/voice-calls.jsonl` | One line per voice event: what was heard, end of speech to first audio (`first_audio_ms`), barge-ins with their stop time (`stop_ms`), backchannels ignored | The gateway, during calls |
| `evals/results/live-metrics.json` | p50/p95 and raw timings of every step (voice, planner, tools, systems) for the run | The gateway, every 15 s and on exit |
| `evals/results/stt-wer.json` | Speech-to-text word error rate on Indian-accented English (Svarah) | `poetry run python evals/stt_wer.py` |
| `scripts/smoke/results/deepgram-*.json`, `summary.md` | Deepgram speech-to-text and text-to-speech smoke tests | `poetry run poe smoke-deepgram` / `smoke-all` |

The first two are git-ignored; delete them to start a fresh session.

## Configuration

All settings are in `.env` (see `.env.example`). Useful ones:

| Setting | Default | Meaning |
| --- | --- | --- |
| `LLM_PROVIDER_CHAIN` | `cerebras-qwen,groq,cerebras,groq-fast,openrouter` | Order of LLM providers (Qwen 3.8 27B on Cerebras first, gpt-oss-120b as fallback); `ollama` for local, `stub` for load tests |
| `VOICE_STT` / `VOICE_TTS` | `deepgram` / `deepgram` | Use `groq` / `deepgram-http` where WebSockets are blocked |
| `LLM_MAX_WAIT_S` | `20` | Longest a turn waits for a rate-limited provider to recover |
| `DECIDER_BACKEND` | `llm` | `jev` to route with TypeSafe JEV (needs `TYPESAFE_API_KEY`), the LLM as fallback |
| `REDIS_URL` | empty (in memory) | Share sessions across several gateway processes |
| `KB_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Hugging Face embedding model for policy search (fastembed, ONNX, CPU) |
| `KB_MIN_SCORE` / `KB_SCOPE_SCORE` | `0.6` / `0.62` | Similarity below which a passage is not relevant / at which an "out of scope" message is treated as a policy question |
| `DEMO_TODAY` | `2026-11-11` | Fixed "today" so data and answers are reproducible |

## Layout

```
core/       platform: orchestrator, Decider, LLM chain, registry, rules, calculators,
            connectors, actions, sessions, tracing, number check, knowledge base (RAG)
packs/      domain packs: loans/, credit_cards/
services/   mock CRM, Loans, Payments, Policy KB, Service Ops, Cards (+ faults.yaml)
gateway/    API, auth, chat WebSocket, session runtime, voice pipeline
web/        browser client (plain HTML, CSS, JavaScript)
data/       seed scripts, policy clauses (policy/), FAQ documents for retrieval (kb/),
            open-data samples and their provenance (open/)
evals/      golden questions, decision set, benchmarks, results
scripts/    run_all, chat, smoke tests, load test, LLM stub, fault demo, reports
docs/       architecture, reports, demo script, task list
tests/      offline tests
```

`docker-compose.yml` is provided for container runs but was not tested in the build
environment; `poetry run poe up` is the tested path.
