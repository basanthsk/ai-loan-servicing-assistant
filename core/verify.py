"""Check that every number in an answer came from a tool result (or the customer).

The model writes the explanation; it must not invent or compute figures. This
check extracts the numbers from an answer and looks for each one in the
evidence: tool results and the customer's own messages. Small counts (up to
12) are allowed, since "3 failed payments" or "2 options" need no source.
Rupee amounts may differ by up to 1 because of rounding.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{2,3})+|\d+)(\.\d+)?(?!\w)")
# Identifiers such as L-1042, T-7781-2610-02, CDS-261111-0001 or LT-4.2 are not quantities.
_ID = re.compile(r"\b[A-Z]{1,4}[-\u2010\u2011\u2013][A-Za-z0-9.\-\u2010\u2011\u2013]*\d")


def numbers_in_text(text: str) -> list[float]:
    out = []
    for m in _NUM.finditer(_ID.sub(" ", text or "")):
        whole = m.group(1).replace(",", "")
        out.append(float(whole + (m.group(2) or "")))
    return out


def _walk(value, out: set[float]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int | float):
        out.add(round(float(value), 2))
    elif isinstance(value, str):
        out.update(round(n, 2) for n in numbers_in_text(value))
    elif isinstance(value, dict):
        for v in value.values():
            _walk(v, out)
    elif isinstance(value, list | tuple):
        for v in value:
            _walk(v, out)


def evidence_numbers(evidence: Iterable) -> set[float]:
    out: set[float] = set()
    for item in evidence:
        _walk(item, out)
    return out


def unverified_numbers(answer: str, evidence: Iterable) -> list[float]:
    known = evidence_numbers(evidence)
    bad = []
    for n in numbers_in_text(answer):
        if n <= 12:
            continue
        if any(abs(n - k) <= (1.0 if n >= 100 else 0.01) for k in known):
            continue
        bad.append(n)
    return bad
