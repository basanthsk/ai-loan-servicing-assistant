"""Capability registry: loads domain packs.

A domain pack is a folder under packs/ that adds a product or workflow without
changing core/:

    tools.yaml      tools the planner can call: backend reads (http) and
                    pack functions (python), with JSON-schema parameters
    actions.yaml    write operations, run only through the action framework
    decisions.yaml  intents for routing, fast paths, Decider thresholds
    rules.yaml      business rules for the rules engine
    prompt.md       scope and tone for this product
    functions.py    optional pack-local functions (calculators, summaries)

Packs are validated with Pydantic when they load, so a broken pack fails at
start-up with a clear message instead of misbehaving in a conversation.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, model_validator

from core.connectors import ConnectorHub
from core.rules import RulesEngine

PACKS_DIR = Path(__file__).resolve().parents[1] / "packs"

_TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool}


class ParamSpec(BaseModel):
    type: Literal["string", "number", "integer", "boolean"] = "string"
    description: str = ""
    enum: list[Any] | None = None
    default: Any = None


class ToolSpec(BaseModel):
    name: str
    description: str
    kind: Literal["http", "python"]
    params: dict[str, ParamSpec] = {}
    required: list[str] = []
    # http tools
    system: str | None = None
    path: str | None = None
    cache_ttl_s: float = 0
    fields: list[str] | None = None  # keep only these keys (per item for lists)
    items_key: str | None = None  # list inside a dict response, e.g. "payments"
    # python tools
    function: str | None = None
    # shown to the customer while the tool runs, and spoken in voice if it is slow
    status: str = "Checking your account"
    slow: bool = False

    @model_validator(mode="after")
    def _check(self):
        if self.kind == "http" and not (self.system and self.path):
            raise ValueError(f"http tool {self.name} needs system and path")
        if self.kind == "python" and not self.function:
            raise ValueError(f"python tool {self.name} needs function")
        missing = [r for r in self.required if r not in self.params]
        if missing:
            raise ValueError(f"tool {self.name}: required params not declared: {missing}")
        return self

    def openai_schema(self) -> dict:
        return _schema(self.name, self.description, self.params, self.required)


class ActionSpec(BaseModel):
    name: str
    description: str
    params: dict[str, ParamSpec] = {}
    required: list[str] = []
    request_type: str
    title: str
    confirm: str  # template filled from params and the prepare function's display values
    requires_otp: bool = True
    prepare: str | None = None  # pack function that validates and enriches the request

    def openai_schema(self) -> dict:
        return _schema(
            self.name,
            self.description + " The customer is asked to confirm first.",
            self.params,
            self.required,
        )


class FastPath(BaseModel):
    function: str  # pack function returning {"say": "..."} plus data


class IntentSpec(BaseModel):
    description: str
    fast_path: FastPath | None = None


class Decisions(BaseModel):
    intents: dict[str, IntentSpec]
    # Regexes that show a message is about this pack ("credit card", "EMI"). Used to
    # spot a message that touches several packs, so the planner gets all their tools.
    mentions: list[str] = []
    fast_path_threshold: float = 0.85
    confirmation_threshold: float = 0.9


def _schema(name: str, description: str, params: dict[str, ParamSpec], required: list[str]) -> dict:
    props = {}
    for k, p in params.items():
        # Optional parameters accept null: some providers validate tool calls strictly.
        prop: dict = {"type": p.type if k in required else [p.type, "null"]}
        if p.description:
            prop["description"] = p.description
        if p.enum:
            prop["enum"] = p.enum
        props[k] = prop
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


def validate_args(params: dict[str, ParamSpec], required: list[str], args: dict) -> list[str]:
    """Return a list of problems with tool arguments (empty when valid)."""
    problems = [f"missing required argument '{r}'" for r in required if args.get(r) in (None, "")]
    for k, v in args.items():
        if k not in params:
            problems.append(f"unknown argument '{k}'")
            continue
        p = params[k]
        if v is None:
            continue
        expected = _TYPES[p.type]
        if p.type in ("number", "integer") and isinstance(v, str):
            try:
                args[k] = v = float(v.replace(",", "")) if p.type == "number" else int(v)
            except ValueError:
                problems.append(f"argument '{k}' must be a {p.type}")
                continue
        if isinstance(v, bool) and p.type != "boolean" or not isinstance(v, expected):
            problems.append(f"argument '{k}' must be a {p.type}")
        elif p.enum and v not in p.enum:
            problems.append(f"argument '{k}' must be one of {p.enum}")
    return problems


@dataclass
class ToolContext:
    """What a pack function can use. customer_id always comes from the session."""

    customer_id: str
    today: date
    hub: ConnectorHub
    rules: RulesEngine
    entities: dict = field(default_factory=dict)

    async def get(self, system: str, path: str, params: dict | None = None, cache_ttl_s: float = 0):
        path = path.replace("{customer_id}", self.customer_id)
        params = dict(params or {})
        if "/customers/" not in path:
            params["customer_id"] = self.customer_id
        return await self.hub[system].get(
            path, customer_id=self.customer_id, params=params, cache_ttl_s=cache_ttl_s
        )

    async def clause(self, clause_id: str) -> dict:
        c = await self.get("policy", f"/clauses/{clause_id}", cache_ttl_s=3600)
        return {"id": c["id"], "title": c["title"], "text": c["text"], "source": c.get("source")}

    def evaluate(self, topic: str, facts: dict) -> dict:
        from core.tracing import span

        with span(f"rules {topic}", "rules") as s:
            verdict = self.rules.evaluate(topic, facts)
            s.attrs.update(rule_id=verdict.rule_id, clause=verdict.clause)
        return verdict.as_dict()


@dataclass
class Pack:
    name: str
    title: str
    description: str
    prompt: str
    tools: dict[str, ToolSpec]
    actions: dict[str, ActionSpec]
    decisions: Decisions
    rules: RulesEngine
    functions: dict[str, Any]
    # Set on a combined pack only: tool or action name -> (owning pack, its own name)
    owners: dict[str, tuple[Any, str]] | None = None
    members: tuple[str, ...] = ()

    def tool_schemas(self) -> list[dict]:
        return [t.openai_schema() for t in self.tools.values()] + [
            a.openai_schema() for a in self.actions.values()
        ]


def combine_packs(packs: list[Pack]) -> Pack:
    """One planner view over several packs, for a message that touches all of them.
    Each tool and action keeps running in its own pack (its rules, functions and
    systems). A name used by more than one pack gets the pack name as a suffix."""
    tools, actions, owners = {}, {}, {}
    for p in packs:
        for items, target in ((p.tools, tools), (p.actions, actions)):
            for name, spec in items.items():
                public = name if name not in owners else f"{name}_{p.name}"
                target[public] = spec.model_copy(update={"name": public})
                owners[public] = (p, name)
    prompt = "\n\n".join(f"## {p.title}\n{p.prompt}" for p in packs)
    prompt = (
        "This message is about more than one product. Answer every part, using the "
        "tools for each product.\n\n" + prompt
    )
    first = packs[0]
    return Pack(
        "+".join(p.name for p in packs),
        " and ".join(p.title for p in packs),
        "; ".join(p.description for p in packs),
        prompt,
        tools,
        actions,
        first.decisions,
        first.rules,
        {},
        owners,
        tuple(p.name for p in packs),
    )


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}


def load_pack(folder: Path) -> Pack:
    name = folder.name
    try:
        tools_doc = _read_yaml(folder / "tools.yaml")
        tools = {t["name"]: ToolSpec(**t) for t in tools_doc.get("tools", [])}
        actions = {
            a["name"]: ActionSpec(**a)
            for a in _read_yaml(folder / "actions.yaml").get("actions", [])
        }
        decisions = Decisions(**_read_yaml(folder / "decisions.yaml"))
        rules = RulesEngine.from_file(folder / "rules.yaml")
        prompt = (folder / "prompt.md").read_text(encoding="utf-8")
        functions = {}
        if (folder / "functions.py").exists():
            module = importlib.import_module(f"packs.{name}.functions")
            functions = getattr(module, "FUNCTIONS", {})
    except Exception as e:  # noqa: BLE001 - surface any pack problem with its name
        raise ValueError(f"Domain pack '{name}' failed to load: {e}") from e

    needed = [t.function for t in tools.values() if t.function]
    needed += [a.prepare for a in actions.values() if a.prepare]
    needed += [i.fast_path.function for i in decisions.intents.values() if i.fast_path]
    missing = [f for f in needed if f not in functions]
    if missing:
        raise ValueError(
            f"Domain pack '{name}' references functions not in functions.py: {missing}"
        )
    clash = set(tools) & set(actions)
    if clash:
        raise ValueError(f"Domain pack '{name}' has tools and actions with the same name: {clash}")

    return Pack(
        name,
        tools_doc.get("title", name),
        tools_doc.get("description", ""),
        prompt,
        tools,
        actions,
        decisions,
        rules,
        functions,
    )


def load_packs(names: list[str] | None = None, folder: Path = PACKS_DIR) -> dict[str, Pack]:
    found = sorted(p for p in folder.iterdir() if (p / "tools.yaml").exists())
    packs = {p.name: load_pack(p) for p in found if names is None or p.name in names}
    intents = [i for pk in packs.values() for i in pk.decisions.intents]
    dupes = {i for i in intents if intents.count(i) > 1}
    if dupes:
        raise ValueError(f"Intent names must be unique across packs: {sorted(dupes)}")
    return packs
