"""Audio capture and WAV replay at native sample rates."""

from __future__ import annotations

import asyncio
import logging
import struct
import wave
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pyaudiowpatch as pyaudio

logger = logging.getLogger(__name__)

AudioChunkCallback = Callable[[bytes, float, str], None]


class LiveAudioStream:
    """Capture from a single WASAPI device in a background thread."""

    def __init__(
        self,
        device_index: int,
        channel: str,
        on_chunk: AudioChunkCallback,
        chunk_duration_ms: int = 100,
    ) -> None:
        self.device_index = device_index
        self.channel = channel
        self.on_chunk = on_chunk
        self.chunk_duration_ms = chunk_duration_ms
        self._pa: Optional[pyaudio.PyAudio] = None
        self._stream: Any = None
        self._running = False
        self.sample_rate = 44100

    def start(self) -> None:
        self._pa = pyaudio.PyAudio()
        info = self._pa.get_device_info_by_index(self.device_index)
        self.sample_rate = int(info["defaultSampleRate"])
        frames_per_buffer = int(self.sample_rate * self.chunk_duration_ms / 1000)
        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=min(int(info["maxInputChannels"]), 2),
            rate=self.sample_rate,
            input=True,
            input_device_index=self.device_index,
            frames_per_buffer=frames_per_buffer,
            stream_callback=self._callback,
        )
        self._running = True
        self._stream.start_stream()
        logger.info(
            "Live audio started channel=%s device=%s rate=%s",
            self.channel,
            self.device_index,
            self.sample_rate,
        )

    def _callback(self, in_data: bytes, frame_count: int, time_info: dict, status: int) -> tuple[bytes, int]:
        if not self._running:
            return (in_data, pyaudio.paComplete)
        # Downmix stereo to mono if needed
        samples = np.frombuffer(in_data, dtype=np.int16)
        if len(samples) > frame_count:
            samples = samples.reshape(-1, 2).mean(axis=1).astype(np.int16)
        mono = samples.tobytes()
        self.on_chunk(mono, float(self.sample_rate), self.channel)
        return (in_data, pyaudio.paContinue)

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None


class WavReplayStream:
    """Replay a WAV file in real time, chunk by chunk."""

    def __init__(
        self,
        wav_path: str | Path,
        channel: str,
        on_chunk: AudioChunkCallback,
        chunk_duration_ms: int = 100,
    ) -> None:
        self.wav_path = Path(wav_path)
        self.channel = channel
        self.on_chunk = on_chunk
        self.chunk_duration_ms = chunk_duration_ms
        self.sample_rate = 16000

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.wav_path.exists():
            raise FileNotFoundError(f"Replay WAV not found: {self.wav_path}")

        with wave.open(str(self.wav_path), "rb") as wf:
            self.sample_rate = wf.getframerate()
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            chunk_frames = int(self.sample_rate * self.chunk_duration_ms / 1000)

            while not stop_event.is_set():
                frames = wf.readframes(chunk_frames)
                if not frames:
                    break
                if channels > 1 and sampwidth == 2:
                    samples = np.frombuffer(frames, dtype=np.int16).reshape(-1, channels)
                    mono = samples.mean(axis=1).astype(np.int16).tobytes()
                else:
                    mono = frames
                self.on_chunk(mono, float(self.sample_rate), self.channel)
                await asyncio.sleep(self.chunk_duration_ms / 1000.0)

        logger.info("Replay finished channel=%s file=%s", self.channel, self.wav_path)


async def run_replay_streams(
    mic_path: str | Path,
    system_path: str | Path,
    on_chunk: AudioChunkCallback,
    stop_event: asyncio.Event,
) -> None:
    mic = WavReplayStream(mic_path, "mic", on_chunk)
    system = WavReplayStream(system_path, "system", on_chunk)
    await asyncio.gather(mic.run(stop_event), system.run(stop_event))
