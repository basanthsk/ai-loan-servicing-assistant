# Evaluation report

**Golden questions: 67 of 69 passed.** Each question runs in a fresh session against the live LLM chain and the mock systems; checks are on exact figures (to the rupee), required words, forbidden claims and proposed actions. Figures were computed from the seeded data and calculators, not by an LLM.

Run on 12 November 2026 on the default chain (Qwen 3.8 27B on Cerebras for every planner and Decider call, no rate-limit errors), on data where the generated customers replay real repayment and card behaviour from the Home Credit Default Risk data (data/open/README.md); the three demo customers are hand-built. 69 questions, including three that ask about a loan and a card in one message (C30-C32), which plan over both packs' tools. The two failures are right in substance but miss the checked wording: L07 says "from 37 to 35 months" rather than "2 months", and C09 lists the Rs 2,999 fee and Rs 540 GST without the Rs 3,539 total. Earlier comparison runs on the first 66 questions are kept alongside: golden-*-gpt-oss-120b.json (56/66 after a rerun, mostly quota outages), golden-*-gpt-5-mini.json (OpenAI GPT-5 mini and nano, 56/66, p50 6-7 s) and golden-*-jev-qwen.json (JEV routing with Qwen planning, 61/66).

## Loans (36/37, run 2026-11-12 02:10 UTC)

| Id | Question | Result | Total ms | Verified | Tools |
| --- | --- | --- | --- | --- | --- |
| L01 | What's my next EMI? | pass | 570 | yes | next_emi |
| L02 | Why did my EMI go up this month? | pass | 1088 | yes | explain_emi_change |
| L03 | Why was my previous EMI different from my current one? | pass | 982 | yes | explain_emi_change |
| L04 | If I make a ₹50,000 prepayment today on my floating loan, how much interest would I save? | pass | 1230 | yes | simulate_prepayment |
| L05 | What happens if I prepay ₹50,000 on L-1042? | pass | 1467 | yes | simulate_prepayment |
| L06 | If I prepay 50,000 on L-1042 and keep the same tenure, what would my new EMI be? | pass | 977 | yes | simulate_prepayment |
| L07 | Would paying ₹20,000 extra on L-1042 reduce my tenure? | FAIL: missing word '2 months|two months' | 1200 | yes | simulate_prepayment |
| L08 | Am I eligible for foreclosure without a penalty on my floating rate loan? | pass | 971 | yes | check_foreclosure |
| L09 | Can I close my fixed rate loan L-1043 without a penalty? | pass | 1303 | yes | check_foreclosure |
| L10 | Is there a prepayment charge on L-1043 if I pay 50,000 now? | pass | 974 | yes | simulate_prepayment |
| L11 | Show me the payments that failed in the last six months and explain what happened. | pass | 2475 | yes | explain_failed_payments |
| L12 | How much did I pay in bounce charges in the last 6 months? | pass | 764 | yes | explain_failed_payments |
| L13 | Why did my September EMI debit fail? | pass | 1127 | yes | explain_failed_payments |
| L14 | Why was there a charge on my August EMI? | pass | 886 | yes | explain_failed_payments |
| L15 | Based on my current loan and applicable terms, what are my options? | pass | 783 | yes | loan_options, loan_options |
| L16 | What is the interest rate on L-1042 now? | pass | 451 | yes |  |
| L17 | How many EMIs are left on L-1042? | pass | 836 | yes | get_loan_details |
| L18 | When is my fixed rate loan's next EMI due? | pass | 266 | yes | next_emi |
| L19 | Please raise a dispute about the June bounce charge, the money was in my account. | pass | 2150 | yes | explain_failed_payments |
| L20 | Email me the repayment statement for L-1042. | pass | 695 | yes |  |
| L21 | I want to prepay 50,000 on L-1042, please submit it. | pass | 1115 | yes |  |
| L22 | What if I prepay ₹20,000 on it? | pass | 898 | yes | simulate_prepayment |
| L23 | Okay, now tell me whether paying ₹20,000 extra would reduce my tenure. | pass | 839 | yes | simulate_prepayment |
| L24 | What's my next EMI? | pass | 258 | yes | next_emi |
| L25 | Why did my EMI increase? | pass | 1202 | yes | explain_emi_change |
| L26 | What happens if I prepay ₹50,000? | pass | 1040 | yes | simulate_prepayment |
| L27 | Did any of my payments fail recently? | pass | 2611 | yes | explain_failed_payments |
| L28 | Can I foreclose my loan without a penalty? | pass | 1449 | yes | check_foreclosure |
| L29 | If I prepay 50,000, is there a charge? | pass | 1304 | yes | simulate_prepayment |
| L30 | Why did my EMI change? | pass | 919 | yes | explain_emi_change |
| L31 | What's the weather in Mumbai today? | pass | 538 | yes | scope_check |
| L32 | Can I move my EMI date to the 10th of the month? | pass | 848 | yes | search_policy |
| L33 | I opened a new bank account. How do I get my EMIs debited from it? | pass | 787 | yes | search_policy |
| L34 | My complaint hasn't been resolved for over a month. What can I do? | pass | 2247 | yes | search_policy, search_policy, search_policy |
| L35 | Will a late EMI affect my credit score? | pass | 1037 | yes | search_policy |
| L36 | When rates go up, can I extend my tenure instead of paying a higher EMI? | pass | 905 | yes | search_policy |
| L37 | Do you offer a gold loan against jewellery? | pass | 637 | yes |  |

## Cards (31/32, run 2026-11-12 02:12 UTC)

| Id | Question | Result | Total ms | Verified | Tools |
| --- | --- | --- | --- | --- | --- |
| C01 | How much do I owe on my credit card? | pass | 358 | yes | card_minimum_due |
| C02 | What's the minimum due on my card? | pass | 281 | yes | card_minimum_due |
| C03 | When is my credit card bill due? | pass | 331 | yes | card_minimum_due |
| C04 | Why is my credit card bill higher this month? | pass | 2592 | yes | explain_bill_change |
| C05 | Why is my card bill so much more than last month? | pass | 1519 | yes | explain_bill_change |
| C06 | Is there anything odd on my latest card statement? | pass | 1624 | yes | explain_bill_change, get_card_transactions |
| C07 | Was I charged twice for anything on my card? | pass | 2501 | yes | explain_bill_change |
| C08 | What was my biggest card purchase last month? | pass | 1083 | yes | get_card_transactions |
| C09 | How much were the fees on my latest card statement? | FAIL: missing number 3539 | 1547 | yes | get_card_transactions, check_annual_fee |
| C10 | Why was I charged an annual fee on my card? | pass | 1189 | yes | check_annual_fee |
| C11 | How much more do I need to spend to get the annual fee waived? | pass | 989 | yes | check_annual_fee |
| C12 | What happens if I pay only the minimum on my card? | pass | 1244 | yes | simulate_minimum_payment |
| C13 | How much interest would I pay if I pay just the minimum due? | pass | 1066 | yes | simulate_minimum_payment |
| C14 | If I pay the full card balance, do I pay any interest? | pass | 1275 | yes | search_policy |
| C15 | What was my previous card statement amount? | pass | 1754 | yes | explain_bill_change |
| C16 | Please dispute the duplicate QuickRide charge on my card. | pass | 3688 | yes | get_card_transactions, explain_bill_change |
| C17 | I lost my credit card, block it now. | pass | 2313 | yes |  |
| C18 | Someone is misusing my card, please block it. | pass | 1218 | yes |  |
| C19 | Yes, dispute the duplicate one. | pass | 1446 | yes | explain_bill_change |
| C20 | And what if I pay only that? | pass | 993 | yes | simulate_minimum_payment |
| C21 | How much did I spend on electronics on my card last month? | pass | 1090 | yes | get_card_transactions |
| C22 | What's the interest rate on my credit card? | pass | 1245 | yes | get_card_details |
| C23 | What's my card's statement balance and when do I need to pay? | pass | 354 | yes | card_minimum_due |
| C24 | What's the minimum due on my credit card? | pass | 593 | yes | card_minimum_due |
| C25 | And how much is due on my card? | pass | 356 | yes | card_minimum_due |
| C26 | Can I change my credit card statement date? | pass | 1140 | yes | search_policy |
| C27 | I want to close my credit card. How long will it take? | pass | 1208 | yes | search_policy |
| C28 | Someone used my card without permission yesterday. Am I liable? | pass | 2728 | yes | search_policy |
| C29 | How much is a reward point worth? | pass | 1133 | yes | search_policy |
| C30 | What's my next EMI and how much is due on my credit card? | pass | 1424 | yes | next_emi, card_minimum_due |
| C31 | Did any of my loan EMIs fail recently, and is there anything odd on my card bill? | pass | 2676 | yes | explain_failed_payments, explain_failed_payments, explain_bill_change |
| C32 | Can I prepay ₹50,000 on L-1042 without a charge, and what is the annual fee on my credit card? | pass | 1734 | yes | check_annual_fee, simulate_prepayment |

## Decider benchmark

118 labelled decisions from our journeys (intent routing across both packs, confirmations including negated and conditional replies, barge-in versus backchannel). Symmetry = same answer when the options are listed in reverse order.

The current set has 183 decisions: 118 written from our journeys and 65 real customer phrasings from two open datasets, BANKING77 (35, CC BY 4.0) and Bitext retail banking (30, CDLA-Sharing 1.0), mapped onto our intents. Rows with 183 cases ran on 11 November 2026 on that set. keyword+llm:cerebras-qwen is Qwen 3.8 27B, the default, with a 1,200-token budget so its reasoning leaves room for the answer. keyword+jev is TypeSafe JEV 1.14, a dedicated decision model billed for input tokens only. Rows with 118 or 108 cases are earlier runs on the journey-only set and are kept for comparison: gpt-oss-120b on Cerebras, GPT-5 nano on OpenAI, and gpt-oss-20b on Groq, whose latency includes pauses a later benchmark fix removed. The real phrasings are harder: both Qwen and JEV fall from about 95% to about 85-87% accuracy.

| Backend | Answered | Accuracy (all) | Accuracy (answered) | Per kind | p50 ms | p95 ms | Symmetry |
| --- | --- | --- | --- | --- | --- | --- | --- |
| keyword | 13/118 | 11% | 100% | intent 0/79, confirmation 5/25, barge_in 8/14 | 0 | 0 | - |
| keyword+llm:groq-fast | 103/108 | 86% | 90% | intent 57/69, confirmation 22/25, barge_in 14/14 | 2982 | 5868 | 31/31 |
| keyword+llm:cerebras | 118/118 | 92% | 92% | intent 72/79, confirmation 23/25, barge_in 14/14 | 632 | 826 | 30/34 |
| keyword+llm:cerebras-qwen | 177/183 | 87% | 90% | intent 121/144, confirmation 24/25, barge_in 14/14 | 371 | 1123 | 52/55 |
| keyword+llm:openai | 117/118 | 90% | 91% | intent 70/79, confirmation 22/25, barge_in 14/14 | 2545 | 3962 | 29/35 |
| keyword+jev | 183/183 | 85% | 85% | intent 118/144, confirmation 23/25, barge_in 14/14 | 144 | 190 | 51/56 |

## Policy retrieval (RAG)

40 customer-style paraphrases of policy questions, each with the passage that answers it, and 4 off-topic questions that should retrieve nothing (`evals/retrieval.yaml`). Embeddings: BAAI/bge-small-en-v1.5 (Hugging Face, run locally); store: chroma (persistent, cosine); 35 passages; similarity cut-off 0.6.

| Hit@1 | Recall@4 | MRR | Off-topic rejected | Passages returned (avg) | Search p50 ms | p95 ms |
| --- | --- | --- | --- | --- | --- | --- |
| 87.5% | 97.5% | 0.919 | 100% | 2.95 | 7.3 | 10.4 |

Questions whose expected passage was not ranked first:

- Do you accept cash for my loan payment? (expected KB-L-03, got LT-6.3, KB-L-10, LT-6.1, KB-L-03)
- rates went up again, can I keep my EMI and extend the loan instead? (expected KB-L-06, got KB-L-10, KB-L-06, PAY-2.1, LT-6.3)
- is it possible to move from floating to fixed interest? (expected KB-L-06, got LT-4.2, KB-L-06, LT-6.1, LT-6.3)
- will a bounced EMI show up on my CIBIL report? (expected KB-L-08, got PAY-2.1, KB-L-08, KB-L-12, PAY-2.2)
- I lost my job and can't afford the EMI right now (expected KB-L-10, got KB-L-03, PAY-2.1, PAY-2.2, KB-L-02)
