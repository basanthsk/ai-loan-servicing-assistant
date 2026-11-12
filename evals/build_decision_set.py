"""Build evals/decisions.jsonl: labelled decisions for the Decider benchmark.

Hand-written cases from our journeys (intent routing, confirmations including
negated and mixed replies, barge-in vs backchannel), plus, with `--with-open-data`,
real customer phrasings from two open datasets, downloaded from Hugging Face:

- BANKING77 (PolyAI, CC BY 4.0): real online-banking queries labelled by intent
- Bitext retail banking (CDLA-Sharing 1.0): banking requests with natural typos

Only intents that map cleanly onto ours are used, a fixed sample per intent
(seeded, so the set is reproducible), and rows containing profanity are dropped.

Run: poetry run python evals/build_decision_set.py --with-open-data
(needs `poetry install --with evals` for pyarrow, to read BANKING77's parquet file)
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import re
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evals" / "decisions.jsonl"
CACHE = ROOT / "evals" / "cache"
HF = "https://huggingface.co/datasets"
PER_INTENT = 5

BANKING77_MAP = {
    "lost_or_stolen_card": "card_block",
    "compromised_card": "card_block",
    "transaction_charged_twice": "card_transactions",
    "card_payment_fee_charged": "card_fees",
    "exchange_rate": "card_policy",  # the forex markup is in the card FAQ (KB-C-08)
    "top_up_failed": "out_of_scope",
    "apple_pay_or_google_pay": "out_of_scope",
}
BITEXT_MAP = {
    "block_card": "card_block",
    "check_card_annual_fee": "card_fees",
    "check_current_balance_on_card": "card_minimum_due",
    "check_loan_payments": "loan_payments",
    "cancel_card": "card_policy",
    "activate_card_international_usage": "card_policy",
}
_PROFANITY = re.compile(r"fuck|shit|damn|crap|<|>", re.I)


def _download(url: str, name: str) -> bytes:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / name
    if not f.exists():
        r = httpx.get(url, follow_redirects=True, timeout=120)
        r.raise_for_status()
        f.write_bytes(r.content)
    return f.read_bytes()


def _sample(texts: list[str], label: str, source: str, rng: random.Random) -> list[dict]:
    clean = sorted({t.strip() for t in texts if t.strip() and not _PROFANITY.search(t)})
    return [
        {"kind": "intent", "text": t, "label": label, "source": source}
        for t in rng.sample(clean, min(PER_INTENT, len(clean)))
    ]


def banking77_rows(rng: random.Random) -> list[dict]:
    import pyarrow.parquet as pq

    readme = _download(f"{HF}/PolyAI/banking77/raw/main/README.md", "banking77-README.md")
    names = dict(re.findall(r"'(\d+)': (\w+)", readme.decode("utf-8")))
    data = _download(
        f"{HF}/PolyAI/banking77/resolve/main/data/test-00000-of-00001.parquet",
        "banking77-test.parquet",
    )
    table = pq.read_table(io.BytesIO(data)).to_pylist()
    rows = []
    for intent, label in BANKING77_MAP.items():
        texts = [r["text"] for r in table if names.get(str(r["label"])) == intent]
        rows += _sample(texts, label, "banking77", rng)
    return rows


def bitext_rows(rng: random.Random) -> list[dict]:
    data = _download(
        f"{HF}/bitext/Bitext-retail-banking-llm-chatbot-training-dataset/resolve/main/"
        "bitext-retail-banking-llm-chatbot-training-dataset.csv",
        "bitext-retail-banking.csv",
    )
    table = list(csv.DictReader(io.StringIO(data.decode("utf-8"))))
    rows = []
    for intent, label in BITEXT_MAP.items():
        texts = [r["instruction"] for r in table if r["intent"] == intent]
        rows += _sample(texts, label, "bitext", rng)
    return rows


INTENT = {
    "loan_next_emi": [
        "What's my next EMI?",
        "When is my loan instalment due?",
        "How much is my next loan payment?",
        "next emi amount please",
        "When do I have to pay my EMI this month?",
    ],
    "loan_emi_change": [
        "Why did my EMI go up?",
        "My EMI increased this month, why?",
        "Why was my previous EMI different from my current one?",
        "Why has my loan interest rate changed?",
        "the emi amount is higher than last month, what happened",
    ],
    "loan_prepayment": [
        "What if I prepay 50,000?",
        "Can I pay extra on my loan to finish early?",
        "Would paying 20,000 extra reduce my tenure?",
        "I want to make a part payment on my loan",
        "how much interest do I save if I prepay",
    ],
    "loan_foreclosure": [
        "Can I close my loan early?",
        "Am I eligible for foreclosure without a penalty?",
        "What does it cost to foreclose my loan?",
        "I want to pay off the whole loan now",
        "is there a penalty for closing the loan",
    ],
    "loan_payments": [
        "Show me the payments that failed in the last six months",
        "Why did my EMI bounce?",
        "Did my last EMI go through?",
        "Was there a charge on my August EMI?",
        "check my last payment first",
    ],
    "loan_dispute": [
        "I want to dispute the bounce charge",
        "The June charge is wrong, please raise a dispute",
        "Raise a complaint about the bounce fee",
        "That 500 rupee charge was not my fault, reverse it",
    ],
    "loan_options": [
        "What are my options on my loan?",
        "Based on my current loan and terms, what can I do?",
        "What choices do I have with my loan?",
    ],
    "loan_statement": [
        "Send me my loan statement",
        "Email me the repayment schedule",
        "I need a statement for my loan for tax",
    ],
    "loan_requests": [
        "What's the status of my dispute?",
        "Has my service request been resolved?",
        "Any update on my complaint?",
    ],
    "card_minimum_due": [
        "What's the minimum due on my card?",
        "How much do I owe on my credit card?",
        "When is my card bill due?",
        "credit card payment due date?",
    ],
    "card_bill_change": [
        "Why is my credit card bill higher this month?",
        "My card statement is much bigger than last month, why?",
        "why did my card bill double",
    ],
    "card_transactions": [
        "Was I charged twice on my card?",
        "What was my biggest card purchase last month?",
        "Show my card transactions",
    ],
    "card_dispute": [
        "Dispute the duplicate charge on my card",
        "I don't recognise a charge on my credit card, dispute it",
        "please raise a dispute for the QuickRide charge",
    ],
    "card_block": [
        "I lost my credit card",
        "Block my card, it was stolen",
        "Someone is misusing my card, block it now",
    ],
    "card_interest": [
        "What if I pay only the minimum on my card?",
        "How much interest will I pay on my card?",
        "What's the interest rate on my credit card?",
    ],
    "card_fees": [
        "Why was I charged an annual fee?",
        "How do I get the annual fee waived?",
        "what fees are on my card",
    ],
    "greeting": ["hello", "Hi there", "thanks, that's all", "good morning"],
    "out_of_scope": [
        "What's the weather in Mumbai?",
        "Tell me a joke",
        "Who won the cricket match yesterday?",
        "Can you book me a flight to Delhi?",
        "What's the best mutual fund to buy?",
    ],
    # General policy questions, answered by retrieval from the policy documents
    "loan_policy": [
        "Can I move my EMI date to the 10th?",
        "How do I change the bank account my EMI is debited from?",
        "Will a late EMI affect my credit score?",
        "Can I switch my loan to a fixed rate?",
        "I need an interest certificate for my taxes",
    ],
    "card_policy": [
        "Can I change my credit card statement date?",
        "How do I close my credit card?",
        "How much is a reward point worth?",
        "Am I liable for a fraudulent transaction on my card?",
        "Can I convert a big card purchase into EMIs?",
    ],
}

CONFIRM = {
    "yes": [
        "yes",
        "Yes please",
        "go ahead",
        "Sure, do it",
        "Yes, that's right, submit it",
        "yep confirm",
        "Okay go ahead with that",
        "please proceed",
    ],
    "no": [
        "no",
        "No, don't",
        "cancel",
        "No thanks, not now",
        "Don't do that",
        "stop, I don't want that",
        "No wait, don't do that yet.",
        "Not now, cancel it",
    ],
    "changed_mind": [
        "Actually, what's my next EMI first?",
        "Wait, make it 30,000 instead",
        "Hold on, which loan is this for?",
        "Actually check my last payment first",
        "Yes but first tell me the charge",
    ],
    "unclear": ["hmm", "maybe", "I'm not sure", "what?"],
}

BARGE = {
    "backchannel": ["mm-hmm", "okay", "right", "uh-huh", "yeah", "I see", "got it", "sure"],
    "interruption": [
        "Actually, check my last payment first",
        "Wait, stop",
        "No, I meant the other loan",
        "Hold on, what about my card?",
        "Sorry, can you repeat the amount?",
        "Stop, that's wrong",
    ],
}


def build(with_open_data: bool) -> list[dict]:
    rows = []
    for label, texts in INTENT.items():
        rows += [{"kind": "intent", "text": t, "label": label, "source": "journeys"} for t in texts]
    for label, texts in CONFIRM.items():
        rows += [
            {
                "kind": "confirmation",
                "text": t,
                "label": label,
                "source": "journeys",
                "context": "Request: Submit a prepayment of ₹50,000 on loan L-1042.",
            }
            for t in texts
        ]
    for label, texts in BARGE.items():
        rows += [
            {"kind": "barge_in", "text": t, "label": label, "source": "journeys"} for t in texts
        ]
    if with_open_data:
        rng = random.Random(77)
        rows += banking77_rows(rng) + bitext_rows(rng)
    return rows


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--with-open-data", action="store_true")
    args = p.parse_args()
    rows = build(args.with_open_data)
    OUT.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    kinds, sources = {}, {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        sources[r["source"]] = sources.get(r["source"], 0) + 1
    print(f"Wrote {len(rows)} decisions to {OUT.relative_to(ROOT)}: {kinds} {sources}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
