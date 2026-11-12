"""Voice: a Pipecat pipeline per call, driving the same orchestrator and session as the chat.

    browser mic --WebRTC--> VAD -> Deepgram STT -> AssistantBridge -> Deepgram TTS --WebRTC--> speaker
                                                       |
                                       SessionRuntime (same session as the chat window)

The bridge:
- starts a turn from each final transcript (channel "voice": short spoken answers)
- speaks the reply sentence by sentence while it streams, cleaned for speech
- says a short filler line when a slow tool starts and nothing has been said yet
- barge-in: when the customer speaks over the assistant with real words, it stops
  the audio and cancels the turn (backend and LLM calls included); short
  acknowledgements like "mm-hmm" or "okay" are ignored
- records end-of-speech to first-audio latency
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from core.decider import KeywordDecider
from core.tracing import METRICS

log = logging.getLogger("gateway.voice")
router = APIRouter()
_handler = SmallWebRTCRequestHandler()

STT_MODEL = "nova-3"
# Domain words the speech-to-text should favour ("payment", not "painting").
KEYTERMS = [
    "EMI",
    "loan",
    "payment",
    "prepayment",
    "prepay",
    "foreclosure",
    "foreclose",
    "bounce charge",
    "mandate",
    "statement",
    "dispute",
    "tenure",
    "repo rate",
    "credit card",
]
VOCABULARY_PROMPT = "Loan servicing call. Words: " + ", ".join(KEYTERMS) + "."
SUSTAINED_SPEECH_S = 0.8  # speech this long over the assistant is a real interruption
TTS_VOICE = "aura-2-thalia-en"
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_CLAUSE = re.compile(r"\s*\[[A-Z]{2,4}(?:[-‑–][A-Z])?[-‑–]\d+(?:\.\d+)?\]")


def speakable(text: str) -> str:
    """Clean text for speech: no clause brackets, no markdown, rupees read naturally."""
    text = _CLAUSE.sub("", text)
    text = re.sub("【[^】]*】", "", text)
    text = text.replace("₹", "Rs ").replace("**", "").replace("•", "")
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text)


def is_backchannel(text: str) -> bool:
    return KeywordDecider().decide("barge_in", text) is not None


VOICE_LOG = Path(
    os.environ.get("VOICE_LOG")
    or Path(__file__).resolve().parents[1] / "evals" / "results" / "voice-calls.jsonl"
)


def log_voice_event(event: str, **fields) -> None:
    """Append one line per voice event (what was heard, how fast the reply started,
    barge-ins), so a test call's results can be shared afterwards."""
    try:
        VOICE_LOG.parent.mkdir(parents=True, exist_ok=True)
        line = {"at": datetime.now().isoformat(timespec="seconds"), "event": event, **fields}
        with VOICE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError as e:  # logging must never break a call
        log.warning("could not write the voice log: %s", e)


class AssistantBridge(FrameProcessor):
    """Connects the voice pipeline to the session runtime shared with the chat."""

    def __init__(self, runtime, greeting: str = ""):
        super().__init__()
        self.runtime = runtime
        self.greeting = greeting
        self.bot_speaking = False
        self.voice_turn = False  # the current turn was started by voice, so speak it
        self.response_open = False
        self.said_something = False
        self.filler_done = False
        self.buffer = ""
        self.user_stopped_at: float | None = None
        self.user_started_at: float | None = None
        self.talk_over_at: float | None = None  # customer started speaking over the assistant
        self.last_heard = ""
        runtime.voice_sinks.append(self.on_event)

    def detach(self) -> None:
        if self.on_event in self.runtime.voice_sinks:
            self.runtime.voice_sinks.remove(self.on_event)

    # --- frames from the pipeline -------------------------------------------------
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            self.bot_speaking = True
            if self.user_stopped_at is not None:
                ms = (time.perf_counter() - self.user_stopped_at) * 1000
                METRICS.record("voice:end of speech to first audio", ms)
                log_voice_event("reply_started", heard=self.last_heard, first_audio_ms=round(ms))
                await self.runtime.emit({"type": "voice_metric", "first_audio_ms": round(ms)})
                self.user_stopped_at = None
            await self.runtime.emit({"type": "voice_state", "state": "speaking"})
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_speaking = False
            await self.runtime.emit({"type": "voice_state", "state": "listening"})
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self.user_started_at = time.perf_counter()
            if self.bot_speaking:
                self.talk_over_at = self.user_started_at
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self.user_stopped_at = time.perf_counter()
            self.user_started_at = None
        elif isinstance(frame, UserSpeakingFrame):
            # Barge in on sustained speech over the assistant. This works even when the
            # speech-to-text has no partial results; short backchannels stay below it.
            started = self.user_started_at
            if (
                self.bot_speaking
                and started
                and time.perf_counter() - started >= SUSTAINED_SPEECH_S
            ):
                self.user_started_at = None
                await self.barge_in()

        if isinstance(frame, InterimTranscriptionFrame):
            # Barge in on real words while the assistant talks or thinks.
            if self._busy() and len(frame.text.split()) >= 2 and not is_backchannel(frame.text):
                await self.barge_in()
            return  # transcripts are not passed on, so TTS never reads them
        if isinstance(frame, TranscriptionFrame):
            text = frame.text.strip()
            if not text:
                return
            if self._busy() and is_backchannel(text):
                log_voice_event("backchannel_ignored", heard=text)
                return  # "mm-hmm" while the assistant talks: keep going
            if self._busy():
                await self.barge_in()
            self._reset_response()
            self.last_heard = text
            log_voice_event("heard", heard=text)
            self.voice_turn = True
            await self.runtime.emit({"type": "voice_state", "state": "thinking"})
            self.runtime.start_turn(text, channel="voice")
            return

        await self.push_frame(frame, direction)

    def _busy(self) -> bool:
        running = self.runtime.task is not None and not self.runtime.task.done()
        return self.bot_speaking or (self.voice_turn and running)

    async def barge_in(self) -> None:
        log.info("barge-in: stopping audio and cancelling the turn")
        await self.broadcast_interruption()  # stops TTS and audio output
        self._reset_response()
        await self.runtime.cancel()  # cancels LLM and backend calls in flight
        if self.talk_over_at is not None:
            ms = (time.perf_counter() - self.talk_over_at) * 1000
            METRICS.record("voice:barge-in, start of talk-over to stop", ms)
            log_voice_event("barge_in", stop_ms=round(ms))
            self.talk_over_at = None
        else:
            log_voice_event("barge_in")

    def _reset_response(self) -> None:
        self.response_open = self.said_something = self.filler_done = False
        self.buffer = ""

    # --- events from the orchestrator ---------------------------------------------
    async def say(self, text: str) -> None:
        await self.push_frame(TTSSpeakFrame(speakable(text)))

    async def _open(self) -> None:
        if not self.response_open:
            self.response_open = True
            await self.push_frame(LLMFullResponseStartFrame())

    async def _speak_sentences(self, final: bool = False) -> None:
        parts = _SENTENCE_END.split(self.buffer)
        ready, self.buffer = (parts, "") if final else (parts[:-1], parts[-1])
        for sentence in ready:
            if sentence.strip():
                await self._open()
                self.said_something = True
                await self.push_frame(LLMTextFrame(speakable(sentence) + " "))

    async def on_event(self, e: dict) -> None:
        kind = e.get("type")
        if kind == "user":
            self.voice_turn = e.get("channel") == "voice"
            return
        if not self.voice_turn:
            return  # typed turns are not spoken
        if kind == "status" and e.get("slow") and not self.said_something and not self.filler_done:
            self.filler_done = True
            await self.say(f"{e['text']}, one moment.")
        elif kind == "token":
            self.buffer += e["text"]
            await self._speak_sentences()
        elif kind == "replace":
            self.buffer = ""
            await self._open()
            await self.push_frame(LLMTextFrame("Let me correct that. " + speakable(e["text"])))
        elif kind in ("done", "cancelled", "error"):
            if kind == "error":
                self.buffer = "Sorry, something went wrong. Please try again."
            if kind != "cancelled":
                await self._speak_sentences(final=True)
            if self.response_open:
                await self.push_frame(LLMFullResponseEndFrame())
            self._reset_response()


def make_speech_services(settings, key: str):
    """Speech services from settings. Returns (stt, tts, aiohttp session or None)."""
    http = None
    if settings.voice_stt == "groq":
        from pipecat.services.groq.stt import GroqSTTService

        stt = GroqSTTService(
            api_key=settings.groq_api_key or "proxy-injected",
            model="whisper-large-v3-turbo",
            settings=GroqSTTService.Settings(prompt=VOCABULARY_PROMPT),
        )
    else:
        stt = DeepgramSTTService(
            api_key=key,
            settings=DeepgramSTTService.Settings(
                model=STT_MODEL, smart_format=True, interim_results=True, keyterm=KEYTERMS
            ),
        )
    if settings.voice_tts == "deepgram-http":
        import aiohttp
        from pipecat.services.deepgram.tts import DeepgramHttpTTSService

        http = aiohttp.ClientSession(trust_env=True)
        tts = DeepgramHttpTTSService(
            api_key=key,
            aiohttp_session=http,
            settings=DeepgramHttpTTSService.Settings(voice=TTS_VOICE),
        )
    else:
        tts = DeepgramTTSService(api_key=key, settings=DeepgramTTSService.Settings(voice=TTS_VOICE))
    return stt, tts, http


async def run_call(connection, runtime, settings) -> None:
    key = settings.deepgram_api_key or "proxy-injected"  # cloud sandbox: injected by its proxy
    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    stt, tts, http = make_speech_services(settings, key)
    first = (runtime.session.customer_name or "").split(" ")[0]
    bridge = AssistantBridge(runtime)
    pipeline = Pipeline(
        [
            transport.input(),
            VADProcessor(vad_analyzer=SileroVADAnalyzer()),
            stt,
            bridge,
            tts,
            transport.output(),
        ]
    )
    task = PipelineTask(
        pipeline, params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000)
    )

    @transport.event_handler("on_client_connected")
    async def on_connected(_transport, _client):
        await runtime.emit({"type": "voice_state", "state": "listening"})
        await bridge.say(f"Hi {first}, I'm your loan assistant. What can I help you with?")

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(_transport, _client):
        await runtime.emit({"type": "voice_state", "state": "ended"})
        await task.cancel()

    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        bridge.detach()
        if http is not None:
            await http.close()


# --- signalling endpoints -----------------------------------------------------------
from gateway.app import A, CurrentSession, state  # noqa: E402  (imported after app is defined)


@router.post("/api/voice/offer")
async def offer(body: dict, session: CurrentSession):
    if "sdp" not in body or "type" not in body:
        raise HTTPException(422, "sdp and type are required")
    runtime = state["runtimes"].get(session)

    async def on_connection(connection):
        asyncio.create_task(run_call(connection, runtime, A().settings))

    return await _handler.handle_web_request(SmallWebRTCRequest.from_dict(body), on_connection)


@router.patch("/api/voice/offer")
async def ice(body: dict, session: CurrentSession):
    await _handler.handle_patch_request(SmallWebRTCPatchRequest(**body))
    return {"ok": True}
