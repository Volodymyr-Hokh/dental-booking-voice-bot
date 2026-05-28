"""Pipecat pipeline that runs one voice session over a WebRTC connection."""

from __future__ import annotations

import logging
import struct
import time

import aiohttp
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    LLMRunFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.loggers.debug_log_observer import DebugLogObserver
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
from pipecat.services.elevenlabs.stt import ElevenLabsSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from calendar_service import CalendarService
from config import settings
from prompts import build_system_prompt
from tools import TOOLS_SCHEMA, make_handlers

logger = logging.getLogger(__name__)


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
                logger.info(
                    "AUDIO IN: frames=%d bytes=%d max_rms=%.1f max_peak=%d sr=%d",
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


async def run_bot(webrtc_connection) -> None:
    """Run one voice session against the given SmallWebRTCConnection."""

    async with aiohttp.ClientSession() as session:
        vad = SileroVADAnalyzer(
            params=VADParams(
                confidence=0.5,
                min_volume=0.05,
                start_secs=0.2,
                stop_secs=0.5,
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

        stt = ElevenLabsSTTService(
            api_key=settings.elevenlabs_api_key,
            aiohttp_session=session,
        )
        tts = ElevenLabsTTSService(
            api_key=settings.elevenlabs_api_key,
            settings=ElevenLabsTTSService.Settings(
                voice=settings.elevenlabs_voice_id,
            ),
        )
        llm = OpenAILLMService(
            api_key=settings.openai_api_key,
            settings=OpenAILLMService.Settings(
                model=settings.openai_model,
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
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
        pipeline = Pipeline(processors)

        observers = []
        if settings.debug:
            observers.append(
                DebugLogObserver(
                    frame_types=(
                        VADUserStartedSpeakingFrame,
                        VADUserStoppedSpeakingFrame,
                        UserStartedSpeakingFrame,
                        UserStoppedSpeakingFrame,
                        TranscriptionFrame,
                    )
                )
            )

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
        await runner.run(task)
