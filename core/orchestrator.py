"""Orchestrator: one turn of the conversation, for text and voice alike.

    customer text
      -> pending action?   confirmation decision (yes / no / changed mind / unclear)
      -> route             Decider picks intent and domain pack
      -> fast path         pack function + template, no planner call
      -> planner loop      streamed LLM call with the pack's tools; tool calls run
                           in parallel; at most MAX_ROUNDS rounds
      -> verify            every number in the answer must come from evidence
      -> events            tokens, status, tool timings, action cards, trace

Events go to an async `emit` callback, so the same turn drives the web chat
and the voice pipeline. Cancelling the task running a turn (barge-in) stops
pending LLM and backend calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import date

from openai import APIError

from core.actions import ActionError, ActionFramework
from core.connectors import ConnectorHub, NotFound, SystemUnavailable
from core.decider import Decider
from core.llm import AllProvidersFailed, ProviderChain
from core.registry import Pack, ToolContext, combine_packs, validate_args
from core.session import Session
from core.settings import Settings
from core.tracing import Trace, span, use_trace
from core.verify import unverified_numbers

log = logging.getLogger(__name__)

Emit = Callable[[dict], Awaitable[None]]
MAX_ROUNDS = 4
# Reasoning models think before they write; a two-part answer needs room for both.
PLANNER_MAX_TOKENS = 1500
RESULT_CHARS = 3000

PLATFORM_INTENTS = {
    "greeting": "greets, thanks or says goodbye, with no question",
    "out_of_scope": "asks about something unrelated to banking, their accounts or our products (e.g. weather, sport, recipes)",
}

PLATFORM_RULES = """You are the servicing assistant of a retail lender, helping {name}. Today is {today}.
Rules:
1. Take every account fact from tool results or the customer context below. Never guess.
2. When a question has several parts, request every tool call you need together in one response; they run in parallel.
3. Never calculate. Use the calculator tools and copy figures exactly; for rupees quote the fields ending in _inr.
4. When you state a rule or a charge, cite its clause id in square brackets, e.g. [LT-6.1].
5. To submit anything, call the matching action tool; the system then asks the customer to confirm. Never say a request is submitted unless a tool result says so.
6. If a system is unavailable, say what you could not check and answer the rest.
7. Only help with this customer's accounts and our products; politely decline anything else.
8. {style}

Customer context (already fetched):
{context}"""

STYLE = {
    "text": "Answer in 2 to 5 short plain sentences. Use a short list only for several items. No tables, no headings.",
    "voice": "This is a phone call: answer in at most 3 short spoken sentences, no lists or symbols, "
    "write amounts like 'Rs 12,926' and dates like '5 December'.",
}


def _trim(value, fields: list[str] | None, items_key: str | None):
    if not fields:
        return value
    pick = lambda d: {k: d[k] for k in fields if k in d}  # noqa: E731
    if items_key and isinstance(value, dict) and isinstance(value.get(items_key), list):
        return {
            **{k: v for k, v in value.items() if k != items_key},
            items_key: [pick(i) for i in value[items_key]],
        }
    if isinstance(value, list):
        return [pick(i) if isinstance(i, dict) else i for i in value]
    return pick(value) if isinstance(value, dict) else value


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        packs: dict[str, Pack],
        chain: ProviderChain,
        decider: Decider,
        hub: ConnectorHub,
    ):
        self.settings, self.packs, self.chain, self.decider, self.hub = (
            settings,
            packs,
            chain,
            decider,
            hub,
        )
        self.actions = ActionFramework(hub)
        self.today = date.fromisoformat(settings.demo_today)
        self.intent_pack = {i: p.name for p in packs.values() for i in p.decisions.intents}
        self.intents = {
            i: spec.description for p in packs.values() for i, spec in p.decisions.intents.items()
        }
        self.intents.update(PLATFORM_INTENTS)
        self._combined_cache: dict[tuple[str, ...], Pack] = {}

    def ctx(self, session: Session, pack: Pack) -> ToolContext:
        return ToolContext(session.customer_id, self.today, self.hub, pack.rules, session.entities)

    # --- session start ----------------------------------------------------------------
    async def prefetch(self, session: Session) -> None:
        """Load profile and each pack's account summary in parallel at login."""

        async def pack_context(p: Pack):
            if "context" not in p.functions:
                return p.name, {}
            try:
                return p.name, await p.functions["context"](self.ctx(session, p))
            except (SystemUnavailable, NotFound) as e:
                return p.name, {"unavailable": str(e)}

        profile, *contexts = await asyncio.gather(
            self.hub["crm"].get(
                f"/customers/{session.customer_id}",
                customer_id=session.customer_id,
                cache_ttl_s=300,
            ),
            *(pack_context(p) for p in self.packs.values()),
        )
        session.profile = profile
        session.customer_name = profile["name"]
        session.phone_last4 = "".join(c for c in profile.get("phone", "") if c.isdigit())[-4:]
        session.accounts = {name: ctx for name, ctx in contexts if ctx}

    # --- a turn -----------------------------------------------------------------------
    async def handle(self, session: Session, text: str, emit: Emit, channel: str = "text") -> str:
        trace = Trace(channel=channel, text=text)
        client_emit = emit

        async def emit(e: dict) -> None:  # measures what the customer actually receives
            if e.get("type") in ("token", "replace"):
                trace.mark("first_token")
            if e.get("type") == "status":
                trace.mark("first_status")
            await client_emit(e)

        info: dict = {
            "evidence": [],
            "intent": None,
            "pack": None,
            "verified": True,
            "unverified": [],
        }
        with use_trace(trace):
            try:
                with span("turn", "turn"):
                    answer = await self._turn(session, text, emit, channel, info)
            except AllProvidersFailed:
                answer = (
                    "I'm sorry, my language service isn't responding right now. "
                    "Your account panel still shows your loans and payments. Please try again in a minute."
                )
                await emit({"type": "token", "text": answer})
            session.add_turn(text, answer)
            await emit(
                {
                    "type": "done",
                    "text": answer,
                    "intent": info["intent"],
                    "pack": info["pack"],
                    "verified": info["verified"],
                    "unverified_numbers": info["unverified"],
                    "explain": self._explain(trace, info),
                    "trace": trace.as_dict(),
                }
            )
            return answer

    async def _turn(self, session: Session, text: str, emit: Emit, channel: str, info: dict) -> str:
        info["evidence"].append(text)
        # Earlier turns are evidence too: the customer's words and the replies already checked.
        info["evidence"].extend(h["content"] for h in session.history)

        # 1. A pending action: is this the confirmation?
        pending = session.pending_action
        if pending and pending.status == "awaiting_otp":
            digits = "".join(c for c in text if c.isdigit())
            if len(digits) == 6:
                return await self.submit_otp(session, digits, emit)
        if pending and pending.status == "awaiting_confirmation":
            pack = self.packs[pending.pack]
            d = await self.decider.decide(
                "confirmation",
                "The assistant asked the customer to confirm a request. How did the customer reply?",
                {
                    "yes": "agrees to go ahead",
                    "no": "declines or cancels",
                    "changed_mind": "asks something else or wants to change the request",
                    "unclear": "reply is unclear",
                },
                text,
                context=f"Request: {pending.summary}",
                default="unclear",
            )
            if d.top == "yes" and _conditional(text):
                # "Yes, but first tell me the charge": never execute on a reply that also asks or
                # sets a condition, whatever the model thinks. Treat it as a new request.
                d.top = "changed_mind"
            if d.top == "yes" and d.confidence >= pack.decisions.confirmation_threshold:
                return await self.confirm_action(session, True, emit)
            if d.top == "no":
                return await self.confirm_action(session, False, emit)
            if d.top == "unclear":
                say = (
                    f"Just to check: {pending.summary} Please say yes to go ahead or no to cancel."
                )
                await emit({"type": "token", "text": say})
                return say
            session.pending_action = None  # changed mind: drop it and handle the new request
            await emit(
                {"type": "action_result", "action_id": pending.action_id, "status": "cancelled"}
            )

        # 2. Route. Meanwhile the planner starts speculatively on the conversation's current
        #    pack with its output held back, so routing does not add to the wait.
        last_reply = next(
            (h["content"] for h in reversed(session.history) if h["role"] == "assistant"), ""
        )
        spec_pack = self.packs[session.active_pack]
        spec_info = {**info, "evidence": list(info["evidence"])}
        held: list[dict] = []
        live = asyncio.Event()
        pending_before = session.pending_action

        async def spec_emit(e: dict) -> None:
            if live.is_set():
                await emit(e)
            else:
                held.append(e)

        spec_task = asyncio.create_task(
            self._plan(session, spec_pack, text, spec_emit, channel, spec_info)
        )
        try:
            route = await self.decider.decide(
                "intent",
                "Which intent does the customer's message express?",
                self.intents,
                text,
                context=f"Previous assistant reply: {last_reply[:200]}" if last_reply else "",
                default="unrouted",  # unusable decision: the planner on the current pack handles it
            )
        except BaseException:
            spec_task.cancel()
            raise
        info["intent"] = route.top
        pack_name = self.intent_pack.get(route.top, session.active_pack)
        platform = route.top in PLATFORM_INTENTS and route.confidence >= 0.7
        if route.top == "out_of_scope":
            # Before refusing, ask the policy knowledge base: a message that names no
            # product ("my complaint is still open") can still be a policy question.
            covered = await self._scope_check(session, text)
            if covered:
                platform = False
                pack_name = covered["pack"]
                info["scope_check"] = covered["id"]
        pack = self.packs[pack_name]
        intent = pack.decisions.intents.get(route.top)
        fast = bool(
            intent and intent.fast_path and route.confidence >= pack.decisions.fast_path_threshold
        )
        involved = [] if platform else self._packs_involved(text, pack_name, route)
        if len(involved) > 1:
            # One message about several products: plan once with all their tools.
            fast = False
            pack = self._combined(involved)
            pack_name = pack.name
            info["packs"] = involved

        if not platform and not fast and pack_name == spec_pack.name:
            # Routing agrees with the speculative planner: release what it has said so far.
            info["pack"] = pack_name
            for e in held:
                await emit(e)
            live.set()
            answer = await spec_task
            info.update({k: v for k, v in spec_info.items() if k not in ("intent", "pack")})
            return answer

        spec_task.cancel()
        try:
            await spec_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - discarded speculative work
            pass
        session.pending_action = pending_before  # undo anything the discarded planner proposed

        if platform:
            say = self._platform_reply(route.top, session, channel)
            await emit({"type": "token", "text": say})
            return say
        info["pack"] = pack_name
        session.active_pack = pack.members[0] if pack.members else pack_name

        # 3. Fast path
        if fast:
            fn = pack.functions[intent.fast_path.function]
            await emit({"type": "status", "text": "Checking your account"})
            with span(intent.fast_path.function, "tool", fast_path=True):
                try:
                    result = await fn(self.ctx(session, pack))
                except (SystemUnavailable, NotFound) as e:
                    result = None
                    log.info("fast path failed (%s); using the planner", e)
            if result and result.get("say"):
                info["evidence"].append(result)
                await emit({"type": "token", "text": result["say"]})
                return result["say"]

        # 4. Planner
        return await self._plan(session, pack, text, emit, channel, info)

    def _packs_involved(self, text: str, routed: str, route) -> list[str]:
        """The routed pack first, then any other pack the message names ("credit card",
        "EMI") or whose intents the Decider gave real weight to."""
        found = [routed]
        for name, p in self.packs.items():
            if name == routed:
                continue
            named = any(re.search(m, text, re.I) for m in p.decisions.mentions)
            weight = sum(
                v for i, v in route.probabilities.items() if self.intent_pack.get(i) == name
            )
            if named or weight >= 0.4:
                found.append(name)
        return found

    def _combined(self, names: list[str]) -> Pack:
        key = tuple(names)
        if key not in self._combined_cache:
            self._combined_cache[key] = combine_packs([self.packs[n] for n in names])
        return self._combined_cache[key]

    async def _scope_check(self, session: Session, text: str) -> dict | None:
        """The best policy passage across all packs, if it matches the message well."""
        if "policy" not in self.hub.connectors:
            return None
        ctx = self.ctx(session, self.packs[session.active_pack])
        try:
            with span("scope_check", "tool"):
                found = await ctx.get("policy", "/search", {"query": text[:500]}, 300)
        except Exception as e:  # noqa: BLE001 - without it we fall back to the router's call
            log.info("scope check unavailable: %s", e)
            return None
        best = (found.get("results") or [None])[0]
        if best and best["score"] >= self.settings.kb_scope_score and best["pack"] in self.packs:
            return best
        return None

    async def _plan(
        self, session: Session, pack: Pack, text: str, emit: Emit, channel: str, info: dict
    ) -> str:
        system = (
            PLATFORM_RULES.format(
                name=session.customer_name or "the customer",
                today=self.today.strftime("%d %B %Y"),
                style=STYLE.get(channel, STYLE["text"]),
                context=json.dumps(
                    {
                        "accounts": (
                            {m: session.accounts.get(m, {}) for m in pack.members}
                            if pack.members
                            else session.accounts.get(pack.name, {})
                        ),
                        "remembered": session.entities,
                    },
                    separators=(",", ":"),
                ),
            )
            + "\n\n"
            + pack.prompt
        )
        messages = [
            {"role": "system", "content": system},
            *session.history,
            {"role": "user", "content": text},
        ]
        info["evidence"].append(session.accounts)
        tools = pack.tool_schemas()
        ctx = self.ctx(session, pack)
        spoken: list[str] = []

        for round_no in range(MAX_ROUNDS):
            last = round_no == MAX_ROUNDS - 1
            with span(f"planner round {round_no + 1}", "llm") as s:
                text_parts, calls = await self._planner_round(messages, tools, last, emit, s)
            spoken.extend(text_parts)
            if not calls:
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": "".join(text_parts) or None,
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": c["arguments"]},
                        }
                        for c in calls
                    ],
                }
            )
            results = await asyncio.gather(
                *(self._run_tool(session, pack, ctx, c, emit) for c in calls)
            )
            for c, r in zip(calls, results, strict=True):
                info["evidence"].append(r)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": c["id"],
                        "content": json.dumps(r, separators=(",", ":"), default=str)[:RESULT_CHARS],
                    }
                )

        if not "".join(spoken).strip():
            # No text yet (e.g. the model spent its budget reasoning): ask once more,
            # without tools, for the answer from the results it already has.
            write_now = {
                "role": "system",
                "content": "Now write the answer to the customer from the tool results above.",
            }
            with span("planner write-up", "llm") as s:
                text_parts, _ = await self._planner_round(
                    [*messages, write_now], tools, True, emit, s
                )
            spoken.extend(text_parts)
            info["write_up_retry"] = True

        answer = "".join(spoken).strip()
        bad = unverified_numbers(answer, info["evidence"])
        if bad:
            answer, bad = await self._regenerate(
                session, pack, ctx, messages, answer, bad, info, emit
            )
            if bad:
                # Never show figures we could not verify: say so instead of guessing.
                info["unverified_draft"] = answer
                answer = UNVERIFIED_FALLBACK
            await emit({"type": "replace", "text": answer})
        if not answer:
            # The model ended without any text (e.g. after a failed tool call).
            info["empty_answer"] = True
            answer = EMPTY_FALLBACK
            await emit({"type": "replace", "text": answer})
        if not bad and answer != EMPTY_FALLBACK:
            cite = _missing_citation(answer, info["evidence"])
            if cite:
                # The answer used retrieved policy text but cited none of it.
                answer += f" [{cite}]"
                info["citation_added"] = cite
                await emit({"type": "token", "text": f" [{cite}]"})
        info["verified"], info["unverified"] = not bad, bad
        return answer

    async def _planner_round(
        self, messages, tools, last: bool, emit: Emit, s
    ) -> tuple[list[str], list[dict]]:
        """One streamed planner call. If the provider rejects the model's tool call
        (e.g. strict schema validation), retry once with a nudge, then without tools."""
        nudge = {
            "role": "system",
            "content": "Your last tool call was rejected as invalid. Call tools "
            "only with valid arguments; leave out optional arguments you don't need.",
        }
        attempts = (
            [(messages, "none")]
            if last
            else [(messages, "auto"), ([*messages, nudge], "auto"), (messages, "none")]
        )
        for i, (msgs, choice) in enumerate(attempts):
            try:
                stream = await self.chain.astream(
                    role="planner",
                    messages=msgs,
                    tools=tools,
                    tool_choice=choice,
                    max_tokens=PLANNER_MAX_TOKENS,
                    temperature=0.2,
                    **({"parallel_tool_calls": True} if choice == "auto" else {}),
                )
                s.attrs.update(provider=stream.provider, model=stream.model)
                return await self._consume(stream, emit)
            except APIError as e:
                if i == len(attempts) - 1:
                    raise
                s.attrs[f"retry_{i + 1}"] = type(e).__name__
                log.warning("planner call rejected (%s); retrying", str(e)[:120])
        raise RuntimeError("unreachable")

    async def _consume(self, stream, emit: Emit) -> tuple[list[str], list[dict]]:
        """Read a streamed reply: forward text as it arrives, assemble tool calls."""
        parts, calls = [], {}
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                parts.append(delta.content)
                await emit({"type": "token", "text": delta.content})
            for tc in delta.tool_calls or []:
                c = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    c["id"] = tc.id
                if tc.function and tc.function.name:
                    c["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    c["arguments"] += tc.function.arguments
        out = [c for _, c in sorted(calls.items())]
        for i, c in enumerate(out):
            c["id"] = c["id"] or f"call_{i}"
        return parts, out

    async def _run_tool(
        self, session: Session, pack: Pack, ctx: ToolContext, call: dict, emit: Emit
    ) -> dict:
        name = call["name"]
        if pack.owners and name in pack.owners:
            # Combined pack: run the tool in the pack it belongs to.
            owner, own_name = pack.owners[name]
            return await self._run_tool(
                session, owner, self.ctx(session, owner), {**call, "name": own_name}, emit
            )
        try:
            args = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError:
            return {
                "error": "arguments were not valid JSON; call the tool again with a JSON object"
            }

        if name in pack.actions:
            spec = pack.actions[name]
            try:
                action = await self.actions.propose(session, pack, spec, args, ctx)
            except ActionError as e:
                return {"error": f"cannot prepare this request: {e}"}
            except SystemUnavailable as e:
                return {
                    "error": f"the {e.system} system is unavailable, so the request cannot be prepared now"
                }
            await emit(
                {
                    "type": "action_required",
                    "action": {
                        "action_id": action.action_id,
                        "title": action.title,
                        "summary": action.summary,
                        "requires_otp": action.requires_otp,
                    },
                }
            )
            return {
                "status": "awaiting_customer_confirmation",
                "summary": action.summary,
                "instruction": "Tell the customer in one sentence what will be submitted and ask them "
                "to confirm. Do not say it is done.",
            }

        spec = pack.tools.get(name)
        if spec is None:
            return {"error": f"unknown tool {name}"}
        problems = validate_args(spec.params, spec.required, args)
        if problems:
            return {"error": "invalid arguments: " + "; ".join(problems)}

        await emit({"type": "status", "text": spec.status, "slow": spec.slow, "tool": name})
        with span(name, "tool", args=args) as s:
            try:
                if spec.kind == "python":
                    result = await pack.functions[spec.function](ctx, **args)
                else:
                    path = spec.path
                    params = {}
                    for k, v in args.items():
                        if "{" + k + "}" in path:
                            path = path.replace("{" + k + "}", str(v))
                        else:
                            params[k] = v
                    result = _trim(
                        await ctx.get(spec.system, path, params, spec.cache_ttl_s),
                        spec.fields,
                        spec.items_key,
                    )
            except SystemUnavailable as e:
                s.ok = False
                result = {
                    "error": f"the {e.system} system is unavailable right now ({e.reason})",
                    "partial_answer_ok": True,
                }
            except NotFound as e:
                s.ok = False
                result = {"error": str(e)}
            except Exception as e:  # noqa: BLE001 - a broken tool must not end the turn
                log.exception("tool %s failed", name)
                s.ok = False
                result = {
                    "error": f"internal error in {name} ({type(e).__name__}); answer without it"
                }
        session.remember(
            **{k: v for k, v in args.items() if k in ("loan_id", "card_id", "payment_id", "amount")}
        )
        await emit({"type": "tool", "name": name, "ok": s.ok, "ms": round(s.duration_ms)})
        return result

    async def _regenerate(
        self,
        session: Session,
        pack: Pack,
        ctx: ToolContext,
        messages: list[dict],
        draft: str,
        bad: list[float],
        info: dict,
        emit: Emit,
    ) -> tuple[str, list[float]]:
        """The draft used figures not found in any tool result. Give the model one more
        chance, with tools, to fetch the exact figures; return the new answer and any
        figures that are still unverified."""
        nums = ", ".join(f"{n:g}" for n in bad)
        msgs = [
            *messages,
            {"role": "assistant", "content": draft},
            {
                "role": "user",
                "content": f"(System check) Your answer used figures not found in any tool result: {nums}. "
                "Call the right tool to get the exact figures, then answer again quoting them "
                "exactly. Do not calculate. Reply with the corrected answer only.",
            },
        ]
        tools = pack.tool_schemas()
        silent = _silent(emit)  # the corrected answer replaces the draft in one piece
        text = ""
        with span("verify rewrite", "llm", unverified=nums):
            for round_no in range(2):
                with span(f"rewrite round {round_no + 1}", "llm") as s:
                    parts, calls = await self._planner_round(msgs, tools, round_no == 1, silent, s)
                text = "".join(parts).strip() or text
                if not calls:
                    break
                msgs.append(
                    {
                        "role": "assistant",
                        "content": "".join(parts) or None,
                        "tool_calls": [
                            {
                                "id": c["id"],
                                "type": "function",
                                "function": {"name": c["name"], "arguments": c["arguments"]},
                            }
                            for c in calls
                        ],
                    }
                )
                results = await asyncio.gather(
                    *(self._run_tool(session, pack, ctx, c, emit) for c in calls)
                )
                for c, r in zip(calls, results, strict=True):
                    info["evidence"].append(r)
                    msgs.append(
                        {
                            "role": "tool",
                            "tool_call_id": c["id"],
                            "content": json.dumps(r, separators=(",", ":"), default=str)[
                                :RESULT_CHARS
                            ],
                        }
                    )
        new = text or draft
        return new, unverified_numbers(new, info["evidence"])

    # --- actions --------------------------------------------------------------------
    async def confirm_action(self, session: Session, approve: bool, emit: Emit) -> str:
        action = session.pending_action
        if action is None:
            say = "There's nothing waiting for confirmation."
        elif not approve:
            action.status = "cancelled"
            session.pending_action = None
            await emit(
                {"type": "action_result", "action_id": action.action_id, "status": "cancelled"}
            )
            say = "Okay, I've cancelled that. Nothing was submitted."
        elif self.actions.needs_otp(session, action):
            action.status = "awaiting_otp"
            code = session.issue_otp()
            await emit(
                {
                    "type": "otp_required",
                    "action_id": action.action_id,
                    "demo_code": code,
                    "phone_last4": session.phone_last4,
                }
            )
            say = (
                f"For security, I've sent a one-time code to your phone ending {session.phone_last4}. "
                "Please enter it, or read it out."
            )
        else:
            return await self._execute(session, emit)
        await emit({"type": "token", "text": say})
        return say

    async def submit_otp(self, session: Session, code: str, emit: Emit) -> str:
        action = session.pending_action
        if action is None or action.status != "awaiting_otp":
            say = "There's no request waiting for a code."
        elif not session.verify_otp(code):
            say = "That code didn't match or has expired. Please try again."
        else:
            return await self._execute(session, emit)
        await emit({"type": "token", "text": say})
        return say

    async def _execute(self, session: Session, emit: Emit) -> str:
        action = session.pending_action
        spec = self.packs[action.pack].actions[action.name]
        try:
            result = await self.actions.execute(session, action, spec.request_type)
        except (SystemUnavailable, ValueError, ActionError) as e:
            await emit(
                {
                    "type": "action_result",
                    "action_id": action.action_id,
                    "status": "failed",
                    "error": str(e),
                }
            )
            say = (
                "I couldn't submit that because our service system isn't responding. Nothing was "
                "submitted; please try again in a few minutes."
            )
            session.pending_action = None
            await emit({"type": "token", "text": say})
            return say
        await emit(
            {
                "type": "action_result",
                "action_id": action.action_id,
                "status": "executed",
                "result": result,
            }
        )
        due = result.get("sla_due")
        due_text = f"{date.fromisoformat(due).day} {date.fromisoformat(due):%B %Y}" if due else ""
        when = (
            "It's complete."
            if result.get("status") == "completed"
            else f"Expected resolution by {due_text}."
        )
        say = f"Done. Your {action.title} is submitted, reference {result['request_id']}. {when}"
        await emit({"type": "token", "text": say})
        return say

    # --- helpers ----------------------------------------------------------------------
    def _platform_reply(self, intent: str, session: Session, channel: str = "text") -> str:
        first = (session.customer_name or "").split(" ")[0]
        if intent == "greeting":
            return (
                f"Hi {first}, I can help with your loans and cards: EMIs, payments, rate changes, "
                "prepayment, foreclosure, disputes and statements. What would you like to know?"
            ).replace("Hi ,", "Hi,")
        if channel == "voice":
            # On a call, "out of scope" is usually a mis-heard word: ask again instead of refusing.
            return "Sorry, I didn't quite catch that. Could you say it again?"
        return (
            "I can only help with your accounts with us, such as loans and cards. "
            "Is there something about those I can help with?"
        )

    def _explain(self, trace: Trace, info: dict) -> dict:
        tools = [
            {
                "name": s.name,
                "ms": round(s.duration_ms),
                "ok": s.ok,
                "args": s.attrs.get("args", {}),
            }
            for s in trace.spans
            if s.kind == "tool"
        ]
        rules = [
            {"rule": s.attrs.get("rule_id"), "clause": s.attrs.get("clause")}
            for s in trace.spans
            if s.kind == "rules"
        ]
        clauses = {}
        for ev in info["evidence"]:
            for c in _find_clauses(ev):
                clauses[c["id"]] = c
        return {
            "tools": tools,
            "rules": rules,
            "clauses": list(clauses.values()),
            "verified": info["verified"],
        }


UNVERIFIED_FALLBACK = (
    "I couldn't confirm the exact figures for that just now, so I won't guess. "
    "Please ask me again in a moment, or check the account panel."
)


EMPTY_FALLBACK = (
    "Sorry, I couldn't put that answer together. Could you ask me again, "
    "perhaps one question at a time?"
)


def _silent(emit: Emit) -> Emit:
    """Pass on status and tool events but hold back text tokens."""

    async def inner(e: dict) -> None:
        if e.get("type") not in ("token", "replace"):
            await emit(e)

    return inner


_CONDITIONAL = re.compile(
    r"\?|\b(but|first|wait|hold on|instead|before|actually|change|only if|unless)\b", re.I
)


def _conditional(text: str) -> bool:
    """A reply that asks something or adds a condition is not a plain yes."""
    return bool(_CONDITIONAL.search(text))


_CITED = re.compile(r"\[([A-Z]{2,4}(?:[-\u2011\u2013][A-Z])?[-\u2011\u2013]\d+(?:\.\d+)?)")


def _missing_citation(answer: str, evidence: list) -> str | None:
    """The best retrieved passage's id, when this turn retrieved policy passages
    (search_policy results) and the answer cites none of them."""
    retrieved = [
        r["results"]
        for r in evidence
        if isinstance(r, dict) and isinstance(r.get("results"), list) and "query" in r
    ]
    passages = [p for rs in retrieved for p in rs if isinstance(p, dict) and "id" in p]
    if not passages:
        return None
    cited = {re.sub("[\u2011\u2013]", "-", c) for c in _CITED.findall(answer)}
    if cited & {p["id"] for p in passages}:
        return None
    return max(passages, key=lambda p: p.get("score", 0))["id"]


def _find_clauses(value) -> list[dict]:
    out = []
    if isinstance(value, dict):
        if {"id", "title", "text"} <= set(value):
            out.append({"id": value["id"], "title": value["title"], "source": value.get("source")})
        for v in value.values():
            out.extend(_find_clauses(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_find_clauses(v))
    return out
