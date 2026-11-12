"""Rules engine: evaluates a domain pack's rules.yaml against facts.

rules.yaml groups rules by topic. Within a topic the first rule whose `when`
condition is true decides; its `then` values are the verdict and its `clause`
is cited. Conditions are small expressions over the facts, for example:

    loan.rate_type == "floating" and borrower.type == "individual"

Expressions are parsed with Python's ast module and only comparisons, boolean
logic, arithmetic, constants and dotted names over the facts are allowed, so a
rules file cannot run code.
"""

from __future__ import annotations

import ast
import operator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

_CMP = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}
_BIN = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


class RuleError(ValueError):
    pass


def _eval(node: ast.AST, facts: dict) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, facts)
    if isinstance(node, ast.BoolOp):
        values = (_eval(v, facts) for v in node.values)
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval(node.operand, facts)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval(node.operand, facts)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, facts)
        for op, comp in zip(node.ops, node.comparators, strict=True):
            right = _eval(comp, facts)
            if type(op) not in _CMP:
                raise RuleError(f"operator {type(op).__name__} not allowed")
            if left is None or right is None:
                if not isinstance(op, ast.Eq | ast.NotEq):
                    return False
            if not _CMP[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        return _BIN[type(node.op)](_eval(node.left, facts), _eval(node.right, facts))
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List | ast.Tuple):
        return [_eval(e, facts) for e in node.elts]
    if isinstance(node, ast.Name):
        if node.id not in facts:
            raise RuleError(f"unknown fact {node.id!r}")
        return facts[node.id]
    if isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            raise RuleError(f"attribute {node.attr!r} not allowed")
        base = _eval(node.value, facts)
        if not isinstance(base, dict):
            raise RuleError(f"cannot read {node.attr!r} from a non-object")
        return base.get(node.attr)
    raise RuleError(f"expression element {type(node).__name__} not allowed")


def evaluate_condition(expr: str, facts: dict) -> bool:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise RuleError(f"invalid condition {expr!r}: {e.msg}") from e
    return bool(_eval(tree, facts))


class Rule(BaseModel):
    id: str
    when: str = "True"
    then: dict
    clause: str | None = None
    explain: str = ""


class RuleSet(BaseModel):
    topics: dict[str, list[Rule]]

    def check(self) -> None:
        """Fail fast on syntax errors when a pack loads."""
        for rules in self.topics.values():
            for r in rules:
                try:
                    ast.parse(r.when, mode="eval")
                except SyntaxError as e:
                    raise RuleError(f"rule {r.id}: invalid condition: {e.msg}") from e


@dataclass
class Verdict:
    topic: str
    rule_id: str
    values: dict
    clause: str | None
    explain: str
    trace: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "topic": self.topic,
            "rule_id": self.rule_id,
            **self.values,
            "clause": self.clause,
            "explain": self.explain,
            "rules_checked": self.trace,
        }


class RulesEngine:
    def __init__(self, ruleset: RuleSet):
        ruleset.check()
        self.ruleset = ruleset

    @classmethod
    def from_file(cls, path: Path) -> RulesEngine:
        data = yaml.safe_load(path.read_text())
        return cls(RuleSet(topics={k: [Rule(**r) for r in v] for k, v in data["topics"].items()}))

    def topics(self) -> list[str]:
        return list(self.ruleset.topics)

    def evaluate(self, topic: str, facts: dict) -> Verdict:
        if topic not in self.ruleset.topics:
            raise RuleError(f"unknown rule topic {topic!r}")
        trace = []
        for rule in self.ruleset.topics[topic]:
            matched = evaluate_condition(rule.when, facts)
            trace.append(f"{rule.id}: {'matched' if matched else 'no match'}")
            if matched:
                return Verdict(topic, rule.id, rule.then, rule.clause, rule.explain, trace)
        raise RuleError(f"no rule in topic {topic!r} matched; add a final catch-all rule")
