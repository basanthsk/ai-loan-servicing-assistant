# Demo script (about 14 minutes)

Before: `poetry run poe seed` and `poetry run poe index`, then `poetry run poe up`, open http://127.0.0.1:8000,
log in as **Priya Sharma**. Keep the "Under the hood" tab in mind for latency.

| # | Show | Say or type | What to point at |
| --- | --- | --- | --- |
| 1 | Accurate answer (J1) | "My EMI increased this month. Why? And if I prepay ₹50,000 today, how much interest would I save?" | Rate reset 10.25% -> 10.75%, EMI ₹12,828 -> ₹12,926 [LT-4.2]; both prepayment options (₹18,215 saved, 5 months sooner). Open **How I got this**: tools, rules, clauses, "numbers verified". |
| 2 | Rules, not retrieval (J3) | "Can I foreclose without a penalty?" then "And my fixed rate loan?" | Floating: no charge even in lock-in (RBI 2025) [LT-6.1]; fixed inside lock-in: 4% charge [LT-6.3]. |
| 3 | Payments + action (J2) | "Show me the payments that failed in the last six months and explain what happened." then "The June charge is wrong, please raise a dispute." | Three causes, two ₹500 charges; confirmation card -> Confirm -> OTP card (demo code shown) -> reference number. Account panel and audit log update. |
| 4 | Voice (J4) | Click the mic. "What's my next EMI?" ... "Actually, check my last payment first." (talk over the answer) ... "Okay, would paying ₹20,000 extra reduce my tenure?" | Barge-in stops the audio; filler line on slow calls; follow-up uses the loan from memory; latency panel shows first audio. |
| 5 | Resilience | Under the hood -> set **payments** to **down**; ask "Which payments failed?" twice more | Partial answer that says Payments is unavailable; circuit breaker opens; calls fail fast. Set it back to normal. |
| 6 | Second workflow (J5) | "Why is my credit card bill higher this month?" then "Yes, dispute the duplicate one." | Same assistant, credit-card pack: bill comparison, duplicate ₹1,299 charge found, dispute with OTP. Show `git show --stat b4ab3f5`: no changes under core/. |
| 7 | Policy questions (RAG) | "Can I move my EMI date to the 10th?" then "My complaint has been open for over a month. What can I do?" | Answer drawn from the policy documents with the passage cited [KB-L-01], [KB-L-11]; **How I got this** shows the retrieved passage. The second message names no product: the router calls it out of scope, and the knowledge-base scope check answers it anyway. Then "Do you offer gold loans?": nothing relevant is retrieved, so the assistant says it has no information instead of guessing. |
| 8 | Evidence | Open docs/eval-report.md and docs/latency-report.md | Golden questions pass rate, Decider benchmark, retrieval accuracy, load test to 1,000 concurrent conversations, fault-injection table. |

If a live LLM call is slow (free-tier rate limits), say so: the chain falls back
to the next provider, and the latency panel shows which one answered.
