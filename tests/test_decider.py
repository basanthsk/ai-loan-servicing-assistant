"""Decider output parsing: unusable model output must raise DecisionError, which the
Decider turns into its default, never crash the turn."""

import pytest

from core.decider import DecisionError, _parse


def test_empty_content_is_a_decision_error():
    # A reasoning model can use its whole token budget thinking and return no content.
    with pytest.raises(DecisionError):
        _parse(None, ["yes", "no"])


def test_broken_json_is_a_decision_error():
    with pytest.raises(DecisionError):
        _parse('{"yes": 0.9, "no": }', ["yes", "no"])


def test_probabilities_are_normalised():
    probs = _parse('Sure: {"yes": 3, "no": 1}', ["yes", "no"])
    assert probs == pytest.approx({"yes": 0.75, "no": 0.25})


async def test_jev_backend_reads_choice_probabilities():
    import httpx

    from core.decider import JevDecider

    seen = {}

    def handler(request):
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "jev-1.14.0",
                "answers": {
                    "d": {
                        "type": "choice",
                        "choice": "changed_mind",
                        "confidence": 1.0,
                        "probabilities": {"yes": 0.1, "changed_mind": 0.9},
                    }
                },
            },
        )

    jev = JevDecider("k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    d = await jev.adecide("Confirm?", ["yes", "no", "changed_mind"], "yes but first...")
    assert d.top == "changed_mind" and d.backend == "jev"
    assert seen["questions"]["d"]["type"] == "choice"
    assert set(seen["questions"]["d"]["criteria"]) == {"yes", "no", "changed_mind"}


async def test_decider_falls_back_when_jev_is_unavailable():
    import httpx

    from core.decider import Decider, JevDecider

    class FakeLLM:
        backend = "llm_json"

        async def adecide(self, question, options, text, context=""):
            from core.decider import _decision

            return _decision(question, {"yes": 1.0, "no": 0.0}, 0, self.backend)

    down = JevDecider(
        "k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
    )
    d = await Decider(down, fallback=FakeLLM()).decide(
        "confirmation", "Confirm?", ["yes", "no"], "sure thing, but quickly"
    )
    assert d.top == "yes" and d.backend == "llm_json"
