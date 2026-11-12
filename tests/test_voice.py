"""Voice bridge logic without audio: transcripts start turns, replies are spoken
sentence by sentence, fillers for slow tools, barge-in and backchannel handling."""

import asyncio

import pytest

pytest.importorskip("pipecat")

from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from gateway.voice import AssistantBridge, speakable  # noqa: E402


class FakeRuntime:
    def __init__(self):
        self.voice_sinks, self.events, self.turns, self.cancelled = [], [], [], 0
        self.task = None

    async def emit(self, e):
        self.events.append(e)

    def start_turn(self, text, channel="text"):
        self.turns.append((text, channel))

        async def long_turn():
            await asyncio.sleep(10)

        self.task = asyncio.get_running_loop().create_task(long_turn())

    async def cancel(self):
        self.cancelled += 1
        if self.task:
            self.task.cancel()


def make_bridge():
    rt = FakeRuntime()
    b = AssistantBridge(rt)
    pushed = []

    async def capture(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)

    b.push_frame = capture
    return b, rt, pushed


def ts(text):
    return TranscriptionFrame(text=text, user_id="u", timestamp="0")


async def test_final_transcript_starts_a_voice_turn_and_is_not_spoken():
    b, rt, pushed = make_bridge()
    await b.process_frame(ts("What's my next EMI?"), FrameDirection.DOWNSTREAM)
    assert rt.turns == [("What's my next EMI?", "voice")]
    assert not any(isinstance(f, TranscriptionFrame) for f in pushed)


async def test_reply_is_spoken_by_sentence_and_cleaned():
    b, rt, pushed = make_bridge()
    await b.on_event({"type": "user", "text": "q", "channel": "voice"})
    for tok in ["Your EMI is ₹12,926 ", "[LT-4.2]. It is due ", "on 5 December."]:
        await b.on_event({"type": "token", "text": tok})
    await b.on_event({"type": "done", "intent": "loan_next_emi"})
    spoken = "".join(f.text for f in pushed if isinstance(f, LLMTextFrame))
    assert "Rs 12,926" in spoken and "LT-4.2" not in spoken and "5 December" in spoken
    assert isinstance(pushed[-1], LLMFullResponseEndFrame)


async def test_typed_turns_are_not_spoken():
    b, rt, pushed = make_bridge()
    await b.on_event({"type": "user", "text": "q", "channel": "text"})
    await b.on_event({"type": "token", "text": "Hello there. "})
    await b.on_event({"type": "done", "intent": "greeting"})
    assert pushed == []


async def test_filler_for_slow_tool_before_any_words():
    b, rt, pushed = make_bridge()
    await b.on_event({"type": "user", "text": "q", "channel": "voice"})
    await b.on_event({"type": "status", "text": "Checking your payment history", "slow": True})
    await b.on_event({"type": "status", "text": "Checking again", "slow": True})
    fillers = [f.text for f in pushed if isinstance(f, TTSSpeakFrame)]
    assert fillers == ["Checking your payment history, one moment."]


async def test_barge_in_on_real_words_stops_audio_and_cancels_turn():
    b, rt, pushed = make_bridge()
    await b.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    await b.process_frame(
        InterimTranscriptionFrame(text="actually check my", user_id="u", timestamp="0"),
        FrameDirection.DOWNSTREAM,
    )
    assert any(isinstance(f, InterruptionFrame) for f in pushed)
    assert rt.cancelled == 1


async def test_backchannel_does_not_interrupt():
    b, rt, pushed = make_bridge()
    await b.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    await b.process_frame(ts("mm-hmm"), FrameDirection.DOWNSTREAM)
    assert not any(isinstance(f, InterruptionFrame) for f in pushed)
    assert rt.turns == [] and rt.cancelled == 0


def test_speakable():
    assert speakable("- EMI ₹12,926 [LT‑6.1]") == "EMI Rs 12,926"


async def test_sustained_speech_over_the_assistant_interrupts():
    import time as _t

    from pipecat.frames.frames import UserSpeakingFrame, VADUserStartedSpeakingFrame

    b, rt, pushed = make_bridge()
    await b.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    await b.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await b.process_frame(UserSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert rt.cancelled == 0  # too short so far: could be "mm-hmm"
    b.user_started_at = _t.perf_counter() - 1.0
    await b.process_frame(UserSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert rt.cancelled == 1
