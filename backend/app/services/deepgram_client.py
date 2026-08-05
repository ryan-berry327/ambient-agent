"""Deepgram live websocket client with auto-reconnect."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional
from urllib.parse import urlencode

import websockets
from websockets.exceptions import ConnectionClosed

from app.config import settings

logger = logging.getLogger(__name__)

TranscriptHandler = Callable[[dict[str, Any], str], Awaitable[None]]
UtteranceEndHandler = Callable[[str], Awaitable[None]]


class DeepgramLiveClient:
    def __init__(
        self,
        channel: str,
        sample_rate: int,
        on_transcript: TranscriptHandler,
        on_utterance_end: UtteranceEndHandler,
        diarize: bool = False,
    ) -> None:
        self.channel = channel
        self.sample_rate = sample_rate
        self.on_transcript = on_transcript
        self.on_utterance_end = on_utterance_end
        self.diarize = diarize
        self._ws: Optional[Any] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=200)
        self._connected = False
        self._bytes_sent = 0

    def _build_url(self) -> str:
        params = {
            "model": "nova-2",
            "encoding": "linear16",
            "sample_rate": str(self.sample_rate),
            "channels": "1",
            "interim_results": "true",
            "smart_format": "true",
            "utterance_end_ms": "1000",
            "vad_events": "true",
            "punctuate": "true",
        }
        if self.diarize:
            params["diarize"] = "true"
        return f"wss://api.deepgram.com/v1/listen?{urlencode(params)}"

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stop.set()
        await self._audio_queue.put(None)
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._ws:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def send_audio(self, chunk: bytes) -> None:
        if self._connected:
            try:
                self._audio_queue.put_nowait(chunk)
            except asyncio.QueueFull:
                logger.warning("Deepgram audio queue full channel=%s", self.channel)

    @property
    def minutes_streamed(self) -> float:
        # 16-bit mono: 2 bytes per sample
        seconds = self._bytes_sent / (2 * self.sample_rate) if self.sample_rate else 0
        return seconds / 60.0

    async def _run_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                headers = {"Authorization": f"Token {settings.deepgram_api_key}"}
                async with websockets.connect(
                    self._build_url(),
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=8 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    backoff = 1.0
                    logger.info("Deepgram connected channel=%s rate=%s", self.channel, self.sample_rate)
                    sender = asyncio.create_task(self._send_loop(ws))
                    receiver = asyncio.create_task(self._recv_loop(ws))
                    done, pending = await asyncio.wait(
                        [sender, receiver],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                    for t in done:
                        exc = t.exception()
                        if exc and not isinstance(exc, asyncio.CancelledError):
                            raise exc
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Deepgram error channel=%s: %s", self.channel, exc)
            finally:
                self._connected = False
                self._ws = None

            if self._stop.is_set():
                break
            logger.info("Deepgram reconnecting channel=%s in %.1fs", self.channel, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _send_loop(self, ws: Any) -> None:
        while not self._stop.is_set():
            chunk = await self._audio_queue.get()
            if chunk is None:
                break
            await ws.send(chunk)
            self._bytes_sent += len(chunk)

    async def _recv_loop(self, ws: Any) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg_type = msg.get("type")
            if msg_type == "Results":
                await self.on_transcript(msg, self.channel)
            elif msg_type == "UtteranceEnd":
                await self.on_utterance_end(self.channel)
