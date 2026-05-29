"""Pipecat pipeline that runs one voice session over a WebRTC connection."""

from __future__ import annotations

import json
import struct
import time
from datetime import datetime
from pathlib import Path

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    LLMRunFrame,
    TTSSpeakFrame,
)
from pipecat.observers.loggers.llm_log_observer import LLMLogObserver
from pipecat.observers.loggers.metrics_log_observer import MetricsLogObserver
from pipecat.observers.loggers.transcription_log_observer import TranscriptionLogObserver
from pipecat.observers.user_bot_latency_observer import (
    LatencyBreakdown,
    UserBotLatencyObserver,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.elevenlabs.stt import (
    CommitStrategy,
    ElevenLabsRealtimeSTTService,
)
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transcriptions.language import Language
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_mute import AlwaysUserMuteStrategy
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from calendar_service import CalendarService
from clinic_info import CLINIC_TZ
from config import settings
from prompts import build_system_prompt
from tools import TOOLS_SCHEMA, make_handlers


def _short_id(pc_id: str | None) -> str:
    """Short, readable session tag derived from the WebRTC connection id."""
    return (pc_id or "unknown").replace("-", "")[:8]


def _format_turn(breakdown: LatencyBreakdown) -> dict:
    """Convert a pipecat LatencyBreakdown into a JSON-friendly turn record.

    Captures wall-clock timing and the STT/LLM/TTS time-to-first-byte split for
    one user→bot cycle (or the opening greeting, where there is no user turn).
    """
    now = datetime.now(CLINIC_TZ)

    user_at = None
    response_secs = None
    if breakdown.user_turn_start_time is not None:
        user_at = datetime.fromtimestamp(breakdown.user_turn_start_time, CLINIC_TZ).isoformat()
        response_secs = round(now.timestamp() - breakdown.user_turn_start_time, 3)

    # Map each per-processor TTFB to a stt/llm/tts bucket (first value wins).
    # Match the acronym + "SERVICE": a bare "STT"/"TTS" substring check is
    # ambiguous because "ElevenLabsTTSService" contains "STT" (…ab*STT*s) and
    # vice-versa, but only "STTSERVICE"/"TTSSERVICE" are unique to each.
    ttfb: dict[str, float] = {}
    for t in breakdown.ttfb:
        name = t.processor.upper()
        for key in ("STT", "LLM", "TTS"):
            if f"{key}SERVICE" in name and key.lower() not in ttfb:
                ttfb[key.lower()] = round(t.duration_secs, 3)
                break

    return {
        "user_at": user_at,
        "bot_at": now.isoformat(),
        "response_secs": response_secs,
        "ttfb": ttfb,
        "user_turn_secs": round(breakdown.user_turn_secs, 3)
        if breakdown.user_turn_secs is not None
        else None,
        "function_calls": [
            {"name": fc.function_name, "secs": round(fc.duration_secs, 3)}
            for fc in breakdown.function_calls
        ],
    }


def _dump_transcript(
    session_id: str,
    context: LLMContext,
    turns: list[dict],
    started_at: datetime,
) -> None:
    """Persist the conversation + timing/latency timeline to logs/transcripts/.

    Best-effort: a failure here must never break session teardown.
    """
    try:
        messages = context.get_messages(truncate_large_values=True)
        payload = {
            "session_id": session_id,
            "started_at": started_at.isoformat(),
            "ended_at": datetime.now(CLINIC_TZ).isoformat(),
            "messages": messages,
            "turns": turns,
        }
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = Path(settings.log_file).parent / "transcripts" / f"{ts}_{session_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info(
            "Transcript saved to {} ({} messages, {} turns)", path, len(messages), len(turns)
        )
    except Exception:
        logger.exception("Failed to dump transcript")


class AudioLevelLogger(FrameProcessor):
    """Diagnostic: log RMS + peak of incoming audio frames once per second.

    Inserted right after transport.input() so we see exactly what bytes Silero
    receives. If RMS is ~0 even while the user is talking, the resampler /
    transport is producing silence and the problem is upstream of VAD.
    """

    def __init__(self):
        super().__init__()
        self._last_log = 0.0
        self._max_rms = 0.0
        self._max_peak = 0
        self._frame_count = 0
        self._byte_count = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            audio = frame.audio
            samples = struct.unpack(f"<{len(audio) // 2}h", audio)
            if samples:
                peak = max(abs(s) for s in samples)
                rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
                self._max_rms = max(self._max_rms, rms)
                self._max_peak = max(self._max_peak, peak)
                self._frame_count += 1
                self._byte_count += len(audio)
            now = time.monotonic()
            if now - self._last_log >= 1.0:
                logger.debug(
                    "AUDIO IN: frames={} bytes={} max_rms={:.1f} max_peak={} sr={}",
                    self._frame_count,
                    self._byte_count,
                    self._max_rms,
                    self._max_peak,
                    frame.sample_rate,
                )
                self._last_log = now
                self._max_rms = 0.0
                self._max_peak = 0
                self._frame_count = 0
                self._byte_count = 0
        await self.push_frame(frame, direction)


class LLMErrorFallback(FrameProcessor):
    """Speak a recovery line when the LLM emits a (non-fatal) ErrorFrame.

    The LLM pushes errors UPSTREAM, so this sits just upstream of the LLM to
    catch them and inject a spoken prompt back downstream (through the LLM, which
    passes non-context frames straight through, to TTS). Without this, a provider
    hiccup — e.g. Groq rejecting a generated tool call — leaves the caller in
    dead silence instead of a graceful "could you say that again?".
    """

    FALLBACK_TEXT = "Sorry, I didn't quite catch that — could you say it again?"

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, ErrorFrame) and not frame.fatal:
            logger.warning("LLM error — speaking recovery prompt: {}", frame.error)
            await self.push_frame(TTSSpeakFrame(self.FALLBACK_TEXT), FrameDirection.DOWNSTREAM)
        await self.push_frame(frame, direction)


async def run_bot(webrtc_connection) -> None:
    """Run one voice session, tagging every log line with its session id."""
    session_id = _short_id(getattr(webrtc_connection, "pc_id", None))
    # contextualize sets a contextvar that propagates into the pipeline's child
    # tasks, so pipecat's own loguru output is tagged with this session too.
    with logger.contextualize(session_id=session_id):
        await _run_session(webrtc_connection, session_id)


async def _run_session(webrtc_connection, session_id: str) -> None:
    """Build and run the pipeline for one connection."""
    vad = SileroVADAnalyzer(
        params=VADParams(
            confidence=0.5,
            min_volume=0.05,
            start_secs=0.2,
            stop_secs=0.3,
        ),
    )
    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=vad,
        ),
    )

    stt = ElevenLabsRealtimeSTTService(
        api_key=settings.elevenlabs_api_key,
        commit_strategy=CommitStrategy.MANUAL,  # pipecat VAD controls turn ends
        settings=ElevenLabsRealtimeSTTService.Settings(
            language=Language.EN,  # force EN, matches the prior batch config
        ),
    )
    tts = ElevenLabsTTSService(
        api_key=settings.elevenlabs_api_key,
        settings=ElevenLabsTTSService.Settings(
            voice=settings.elevenlabs_voice_id,
        ),
    )
    llm = GroqLLMService(
        api_key=settings.groq_api_key,
        settings=GroqLLMService.Settings(
            model=settings.groq_model,
        ),
    )

    calendar = CalendarService(
        calendar_id=settings.google_calendar_id,
        service_account_path=settings.google_service_account_json,
    )

    handlers = make_handlers(calendar)
    for name, handler in handlers.items():
        llm.register_function(name, handler)

    context = LLMContext(
        messages=[{"role": "system", "content": build_system_prompt()}],
        tools=TOOLS_SCHEMA,
    )
    # Use VAD-timeout end-of-turn detection instead of the default Smart Turn v3
    # ML analyzer (which underdetects short/quiet user utterances and never
    # triggers STT, leaving the bot silent after the greeting).
    user_params = LLMUserAggregatorParams(
        user_turn_strategies=UserTurnStrategies(
            stop=[SpeechTimeoutUserTurnStopStrategy()],
        ),
        # TODO(twilio): remove once on telephony. This mutes the caller while the
        # bot speaks to stop speaker echo (the bot's own voice looping into the
        # mic and self-interrupting) in the browser/speaker demo — browser AEC
        # isn't enough for loud speakers. On Twilio the carrier handles echo, so
        # drop this and restore barge-in.
        user_mute_strategies=[AlwaysUserMuteStrategy()],
    )
    context_aggregator = LLMContextAggregatorPair(context, user_params=user_params)

    # Diagnostics (per-frame audio levels + VAD/transcription frame logging)
    # are only wired in when LOG_LEVEL=DEBUG, so a normal demo run keeps
    # clean logs and skips the per-frame Python audio analysis on the hot path.
    processors = [transport.input()]
    if settings.debug:
        processors.append(AudioLevelLogger())
    processors += [
        VADProcessor(vad_analyzer=vad),
        stt,
        context_aggregator.user(),
        LLMErrorFallback(),  # upstream of llm: catches its upstream ErrorFrames
        llm,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ]
    pipeline = Pipeline(processors)

    # Pipecat's built-in observers give per-turn transcript, LLM activity and
    # metrics. They're verbose, so they're only attached on LOG_LEVEL=DEBUG;
    # a normal INFO run shows just key events (connect, booking, errors).
    observers = []
    if settings.debug:
        observers += [
            TranscriptionLogObserver(),
            LLMLogObserver(),
            MetricsLogObserver(),
        ]

    # Per-turn timing + STT/LLM/TTS latency for the transcript. The observer
    # is silent (emits events only), so it's safe to attach at any log level.
    started_at = datetime.now(CLINIC_TZ)
    turns: list[dict] = []
    if settings.transcript_log:
        latency_observer = UserBotLatencyObserver()

        @latency_observer.event_handler("on_latency_breakdown")
        async def _on_breakdown(_obs, breakdown: LatencyBreakdown):
            turns.append(_format_turn(breakdown))

        observers.append(latency_observer)

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
        ),
        observers=observers,
    )

    @transport.event_handler("on_client_connected")
    async def _on_connected(_t, _client):
        logger.info("Client connected — greeting caller")
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_t, _client):
        logger.info("Client disconnected — cancelling task")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    finally:
        if settings.transcript_log:
            _dump_transcript(session_id, context, turns, started_at)
