"""Local Whisper + energy VAD transcription (free, offline)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Optional

import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

TranscriptHandler = Callable[[str, str, bool], Awaitable[None]]
UtteranceEndHandler = Callable[[str], Awaitable[None]]

VAD_RATE = 16000
FRAME_MS = 30
FRAME_BYTES = int(VAD_RATE * FRAME_MS / 1000) * 2  # 16-bit mono
SILENCE_FRAMES_END = 25  # ~750ms silence ends utterance
MIN_SPEECH_FRAMES = 5  # ~150ms minimum speech
SPEECH_RMS_THRESHOLD = 400  # tune for mic/system levels

_whisper_model = None


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel

        compute = "int8" if settings.whisper_device == "cpu" else "float16"
        logger.info(
            "Loading Whisper model=%s device=%s compute=%s",
            settings.whisper_model,
            settings.whisper_device,
            compute,
        )
        _whisper_model = WhisperModel(
            settings.whisper_model,
            device=settings.whisper_device,
            compute_type=compute,
        )
    return _whisper_model


def _resample_pcm16(pcm: bytes, from_rate: int, to_rate: int = VAD_RATE) -> bytes:
    if from_rate == to_rate:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16)
    if len(samples) == 0:
        return pcm
    new_len = max(1, int(len(samples) * to_rate / from_rate))
    x_old = np.linspace(0.0, 1.0, len(samples))
    x_new = np.linspace(0.0, 1.0, new_len)
    resampled = np.interp(x_new, x_old, samples.astype(np.float64)).astype(np.int16)
    return resampled.tobytes()


def _is_speech_frame(frame: bytes) -> bool:
    samples = np.frombuffer(frame, dtype=np.int16)
    if len(samples) == 0:
        return False
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    return rms > SPEECH_RMS_THRESHOLD


def _transcribe_pcm16_sync(pcm: bytes, sample_rate: int = VAD_RATE) -> str:
    if len(pcm) < FRAME_BYTES * MIN_SPEECH_FRAMES:
        return ""
    model = _get_whisper_model()
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = model.transcribe(
        audio,
        language="en",
        vad_filter=False,
        beam_size=1,
        best_of=1,
    )
    return " ".join(seg.text.strip() for seg in segments if seg.text.strip()).strip()


def transcribe_wav_file(path: str | Path) -> str:
    """Transcribe an entire WAV file (used at replay end for complete text)."""
    import wave

    path = Path(path)
    with wave.open(str(path), "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
        rate = wf.getframerate()
    pcm16 = _resample_pcm16(pcm, rate, VAD_RATE)
    return _transcribe_pcm16_sync(pcm16, VAD_RATE)


class WhisperTranscriptionClient:
    """Per-channel energy-VAD + Whisper pipeline."""

    def __init__(
        self,
        channel: str,
        sample_rate: int,
        on_transcript: TranscriptHandler,
        on_utterance_end: UtteranceEndHandler,
    ) -> None:
        self.channel = channel
        self.sample_rate = sample_rate
        self.on_transcript = on_transcript
        self.on_utterance_end = on_utterance_end
        self._running = False
        self._bytes_processed = 0
        self._pcm_buffer = bytearray()
        self._utterance_pcm = bytearray()
        self._in_speech = False
        self._silence_frames = 0
        self._interim_sent = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._running = True
        logger.info("Whisper transcription started channel=%s rate=%s", self.channel, self.sample_rate)

    async def flush(self) -> None:
        """Finalize any in-progress utterance (e.g. when replay ends)."""
        if self._in_speech or self._utterance_pcm:
            await self._finalize_utterance()

    async def stop(self) -> None:
        self._running = False
        if self._utterance_pcm:
            await self._finalize_utterance()
        logger.info("Whisper transcription stopped channel=%s", self.channel)

    async def send_audio(self, chunk: bytes) -> None:
        if not self._running:
            return
        self._bytes_processed += len(chunk)
        resampled = _resample_pcm16(chunk, self.sample_rate, VAD_RATE)
        self._pcm_buffer.extend(resampled)
        while len(self._pcm_buffer) >= FRAME_BYTES:
            frame = bytes(self._pcm_buffer[:FRAME_BYTES])
            del self._pcm_buffer[:FRAME_BYTES]
            await self._process_frame(frame)

    @property
    def minutes_streamed(self) -> float:
        seconds = self._bytes_processed / (2 * self.sample_rate) if self.sample_rate else 0
        return seconds / 60.0

    async def _process_frame(self, frame: bytes) -> None:
        is_speech = _is_speech_frame(frame)
        if is_speech:
            if not self._in_speech and not self._interim_sent:
                await self.on_transcript(self.channel, "…", False)
                self._interim_sent = True
            self._in_speech = True
            self._silence_frames = 0
            self._utterance_pcm.extend(frame)
        elif self._in_speech:
            self._silence_frames += 1
            self._utterance_pcm.extend(frame)
            if self._silence_frames >= SILENCE_FRAMES_END:
                await self._finalize_utterance()

    async def _finalize_utterance(self) -> None:
        pcm = bytes(self._utterance_pcm)
        self._utterance_pcm.clear()
        self._in_speech = False
        self._silence_frames = 0
        self._interim_sent = False

        if len(pcm) < FRAME_BYTES * MIN_SPEECH_FRAMES:
            return

        loop = self._loop or asyncio.get_running_loop()
        text = await loop.run_in_executor(None, _transcribe_pcm16_sync, pcm, VAD_RATE)
        if text:
            await self.on_transcript(self.channel, text, True)
            await self.on_utterance_end(self.channel)
