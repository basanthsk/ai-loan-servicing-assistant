"""Decider: one-call decisions over a fixed set of options, with a probability for each.

Used for intent routing, action confirmation, barge-in vs backchannel and scope
checks. Backends share one interface, so a dedicated decision model can replace
the LLM backend after the benchmark without changing callers.

Backends here:
- KeywordDecider: instant, for clear replies ("yes", "cancel", "mm-hmm"). It
  answers only when it is sure and otherwise defers.
- LLMJsonDecider: asks an LLM (fast role) for a JSON object of option
  probabilities and normalises it.
- JevDecider: TypeSafe's JEV decision model (a "choice" question), which returns
  the probabilities directly; billed for input tokens only.
- Decider: tries the keyword backend first, then the model backend, then an
  optional fallback backend when the first one is unavailable.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import httpx

from core.llm import ProviderChain
from core.tracing import span

SYSTEM = (
    "You classify a customer's message for a bank's servicing assistant. "
    "Return ONLY a compact one-line JSON object mapping the options that could apply (at most 3) "
    "to probabilities between 0 and 1 that sum to 1; options left out count as 0. "
    "Use exactly the option names given. No other text."
)

# Reasoning models think before answering; too small a budget leaves no answer.
DECIDER_MAX_TOKENS = 1200

Options = list[str] | dict[str, str]


@dataclass
class Decision:
    question: str
    probabilities: dict[str, float]
    top: str
    confidence: float
    ms: float
    backend: str
    provider: str = ""

    def is_confident(self, threshold: float) -> bool:
        return self.confidence >= threshold


class DecisionError(RuntimeError):
    pass


def _names(options: Options) -> list[str]:
    return list(options) if isinstance(options, dict) else list(options)


def _parse(text: str | None, options: list[str]) -> dict[str, float]:
    text = text or ""  # a reasoning model can spend its whole budget thinking: no content
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise DecisionError(f"No JSON object in model output: {text[:120]!r}")
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise DecisionError(f"Unparseable JSON in model output: {text[:120]!r}") from e
    probs = {o: max(0.0, float(raw.get(o, 0.0))) for o in options}
    total = sum(probs.values())
    if total <= 0:
        raise DecisionError(f"Model gave no probability to any option: {raw}")
    return {o: p / total for o, p in probs.items()}


def _decision(question, probs, t0, backend, provider="") -> Decision:
    top = max(probs, key=probs.get)
    return Decision(
        question, probs, top, probs[top], (time.perf_counter() - t0) * 1000, backend, provider
    )


class LLMJsonDecider:
    backend = "llm_json"

    def __init__(self, chain: ProviderChain):
        self.chain = chain

    def _messages(self, question: str, options: Options, text: str, context: str) -> list[dict]:
        if isinstance(options, dict):
            opts = "\n".join(f"- {k}: {v}" for k, v in options.items())
        else:
            opts = ", ".join(options)
        prompt = f"Question: {question}\nOptions:\n{opts}\n"
        if context:
            prompt += f"Context: {context}\n"
        prompt += f'Customer message: "{text}"'
        return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]

    def decide(self, question: str, options: Options, text: str, context: str = "") -> Decision:
        t0 = time.perf_counter()
        result = self.chain.chat(
            role="fast",
            messages=self._messages(question, options, text, context),
            max_tokens=DECIDER_MAX_TOKENS,
            temperature=0,
        )
        probs = _parse(result.response.choices[0].message.content, _names(options))
        return _decision(question, probs, t0, self.backend, result.provider)

    async def adecide(
        self, question: str, options: Options, text: str, context: str = ""
    ) -> Decision:
        t0 = time.perf_counter()
        result = await self.chain.achat(
            role="fast",
            messages=self._messages(question, options, text, context),
            max_tokens=DECIDER_MAX_TOKENS,
            temperature=0,
        )
        probs = _parse(result.response.choices[0].message.content, _names(options))
        return _decision(question, probs, t0, self.backend, result.provider)


class JevDecider:
    """TypeSafe JEV: one "choice" question per decision, probabilities per option."""

    backend = "jev"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.typesafe.ai",
        model: str = "jev-latest",
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ):
        self.model = model
        self.url = base_url.rstrip("/") + "/v1/systemone"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.client = client or httpx.AsyncClient(timeout=timeout)

    def _body(self, question: str, options: Options, text: str, context: str) -> dict:
        names = _names(options)
        criteria = (
            {k: v for k, v in options.items()}
            if isinstance(options, dict)
            else {o: o.replace("_", " ") for o in names}
        )
        state = (f"{context}\n" if context else "") + f'Customer message: "{text}"'
        return {
            "model": self.model,
            "state": state,
            "questions": {"d": {"type": "choice", "instructions": question, "criteria": criteria}},
        }

    async def adecide(
        self, question: str, options: Options, text: str, context: str = ""
    ) -> Decision:
        t0 = time.perf_counter()
        try:
            r = await self.client.post(
                self.url, json=self._body(question, options, text, context), headers=self.headers
            )
            r.raise_for_status()
            answer = r.json()["answers"]["d"]
            raw = answer["probabilities"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as e:
            raise DecisionError(f"JEV unavailable: {type(e).__name__}: {e}") from e
        probs = {o: max(0.0, float(raw.get(o, 0.0))) for o in _names(options)}
        total = sum(probs.values())
        if total <= 0:
            raise DecisionError(f"JEV gave no probability to any option: {raw}")
        probs = {o: v / total for o, v in probs.items()}
        return _decision(question, probs, t0, self.backend, "typesafe")


# Whole-message patterns only: "yes, but first tell me X" must not count as a plain yes.
_YES = r"(yes|yeah|yep|yup|sure|ok|okay|confirm(ed)?|go ahead|do it|please do|proceed|yes please|that'?s right|correct)"
_NO = r"(no|nope|nah|cancel|don'?t|do not|stop|not now|no thanks|never ?mind)"
_BACKCHANNEL = r"(mm+-?hmm+|uh-?huh|hmm+|ok(ay)?|right|yeah|i see|got it|sure|go on|mhm)"
_END = r"[\s.!,]*(please)?[\s.!]*$"


class KeywordDecider:
    """Instant decisions for unambiguous short replies; defers (returns None) otherwise."""

    backend = "keyword"

    def decide(self, kind: str, text: str) -> dict[str, float] | None:
        t = text.strip().lower()
        if kind == "confirmation":
            if re.fullmatch(rf"{_YES}{_END}", t):
                return {"yes": 1.0, "no": 0.0, "changed_mind": 0.0, "unclear": 0.0}
            if re.fullmatch(rf"{_NO}{_END}", t):
                return {"yes": 0.0, "no": 1.0, "changed_mind": 0.0, "unclear": 0.0}
        if kind == "barge_in" and re.fullmatch(rf"{_BACKCHANNEL}{_END}", t):
            return {"interruption": 0.0, "backchannel": 1.0}
        return None


class Decider:
    """Keyword pre-check, then the LLM. Records a 'decider' span for the latency panel."""

    def __init__(self, llm, fallback=None):
        """`llm` is the model backend (LLMJsonDecider or JevDecider); `fallback`, if
        given, is tried when it raises DecisionError (e.g. JEV unreachable)."""
        self.keyword = KeywordDecider()
        self.llm = llm
        self.fallback = fallback

    async def decide(
        self,
        kind: str,
        question: str,
        options: Options,
        text: str,
        context: str = "",
        default: str | None = None,
    ) -> Decision:
        """`default` is returned with confidence 0 if the model's answer can't be used,
        so a bad decision degrades the turn instead of failing it."""
        with span(kind, "decider") as s:
            t0 = time.perf_counter()
            probs = self.keyword.decide(kind, text)
            if probs is not None and set(probs) == set(_names(options)):
                d = _decision(question, probs, t0, self.keyword.backend)
            else:
                try:
                    try:
                        d = await self.llm.adecide(question, options, text, context)
                    except (DecisionError, ValueError) as e:
                        if self.fallback is None:
                            raise
                        s.attrs["backend_failed"] = f"{self.llm.backend}: {type(e).__name__}"
                        d = await self.fallback.adecide(question, options, text, context)
                except (DecisionError, ValueError) as e:
                    if default is None:
                        raise
                    s.attrs["fallback"] = type(e).__name__
                    d = Decision(
                        question,
                        {default: 0.0},
                        default,
                        0.0,
                        (time.perf_counter() - t0) * 1000,
                        "fallback",
                    )
            s.attrs.update(top=d.top, confidence=round(d.confidence, 2), backend=d.backend)
            return d
