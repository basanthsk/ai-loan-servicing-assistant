# Open data used for seeding and evaluation

The demo's account data mixes hand-built scenarios with **real behaviour from open
datasets**. Nothing here identifies a person: only behaviour ratios are kept, with no
ids, names, dates or amounts from the source.

| Dataset | Licence | Used for | File |
| --- | --- | --- | --- |
| [Home Credit Default Risk](https://www.kaggle.com/competitions/home-credit-default-risk), via the Hugging Face mirror [mohameddhameem/home-credit-default-risk](https://huggingface.co/datasets/mohameddhameem/home-credit-default-risk) | Kaggle competition data. The mirror is tagged Apache-2.0, but the original terms are the competition's, so treat it as research and demo use only | Repayment behaviour of the 47 generated customers' loans, and six months of credit-card behaviour for 15 generated cards | `home_credit_loan_histories.csv`, `home_credit_card_months.csv` |
| [BANKING77](https://huggingface.co/datasets/PolyAI/banking77) (PolyAI) | CC BY 4.0 | 35 real customer banking queries in the Decider benchmark | `evals/decisions.jsonl` (`source: banking77`) |
| [Bitext retail banking](https://huggingface.co/datasets/bitext/Bitext-retail-banking-llm-chatbot-training-dataset) | CDLA-Sharing 1.0 | 30 banking requests with natural typos in the Decider benchmark | `evals/decisions.jsonl` (`source: bitext`) |
| [RBI directions and circulars](https://www.rbi.org.in), RBL Bank foreclosure communication | Public documents | Policy rules and clause text | `data/policy/*.yaml`, `data/kb/*.md` |

## How the Home Credit sample is used

`extract_home_credit.py` takes a deterministic sample (a hash of the source id
orders the candidates) of:

- **80 real instalment loans** with complete schedules of 24 to 60 instalments (2,161
  instalments). For each instalment we keep how many days late it was paid and the
  share of the instalment paid. 4.1% of instalments were paid late; 21 of the 80 loans
  had at least one late instalment.
- **30 real credit-card accounts**, six consecutive months each: balance, drawings,
  payments and minimum instalment as shares of the credit limit, and days past due.

`data/seed.py` replays them:

- **Loans:** each generated loan takes one real history in turn. Instalment *n* is
  paid on time if the real instalment *n* was. Otherwise the first debit fails and the
  payment succeeds as many days later as the real one did. The source records *when*
  a payment was made, not *why* it was late, so the reason is our mapping, consistent
  with policy PAY-2.1:
  - 1-3 days late: bank technical error, no charge.
  - 4-10 days late: insufficient funds, ₹500 bounce charge.
  - Over 10 days late: expired mandate, ₹500 bounce charge.
- **Cards:** each generated card replays six real months as the May-October 2026
  statements, scaled to the card's credit limit.
  - Spending, repayments and balances follow the real account.
  - Any growth beyond spending minus repayments is booked as interest and charges.
  - Merchants and dates are generated.

Loan amounts, rates, tenures and the customers' names and contacts stay generated.
The source has no Indian-market terms, and its amounts are in another currency.

## Not from open data

The three demo customers (C1001 Priya Sharma, C1002 Rahul Verma, C1003 Ananya Iyer)
are hand-built. The journeys need specific scenarios, such as a rate reset, a duplicate
card charge and a fixed-rate loan in lock-in, and the golden questions check their
exact figures.

## Regenerate

```bash
poetry install --with evals
poetry run python data/open/extract_home_credit.py
poetry run python evals/build_decision_set.py --with-open-data
poetry run poe seed
```
