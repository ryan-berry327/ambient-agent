"""Session orchestration: audio, Deepgram, distiller."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.config import settings, BACKEND_DIR
from app.database import (
    SessionLocal,
    SessionModel,
    SessionStatus,
    TranscriptSegmentModel,
    new_session_id,
    utcnow,
)
from app.services.audio_capture import LiveAudioStream, run_replay_streams
from app.services.audio_devices import list_devices
from app.services.whisper_transcription import WhisperTranscriptionClient
from app.services.distiller import distiller
from app.services.ws_hub import ws_hub

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(self) -> None:
        self._session_id: Optional[str] = None
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._mic_stream: Optional[LiveAudioStream] = None
        self._system_stream: Optional[LiveAudioStream] = None
        self._whisper_mic: Optional[WhisperTranscriptionClient] = None
        self._whisper_system: Optional[WhisperTranscriptionClient] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._sample_rates: dict[str, int] = {"mic": 16000, "system": 16000}
        self._force_distill_task: Optional[asyncio.Task] = None
        self._replay_mode = False
        self._replay_mic_path: Optional[Path] = None
        self._replay_system_path: Optional[Path] = None

    @property
    def active_session_id(self) -> Optional[str]:
        return self._session_id

    def get_state(self) -> dict[str, Any]:
        db = SessionLocal()
        try:
            if self._session_id:
                session = db.get(SessionModel, self._session_id)
                if session:
                    from app.services.cost_tracker import estimate_cost_usd

                    dg_min = session.deepgram_minutes
                    if self._whisper_mic:
                        dg_min += self._whisper_mic.minutes_streamed
                    if self._whisper_system:
                        dg_min += self._whisper_system.minutes_streamed
                    return {
                        "session_id": session.id,
                        "status": session.status,
                        "spec_version": session.spec_version,
                        "replay_mode": session.replay_mode,
                        "deepgram_minutes": round(dg_min, 3),
                        "haiku_input_tokens": session.haiku_input_tokens,
                        "haiku_output_tokens": session.haiku_output_tokens,
                        "estimated_cost_usd": estimate_cost_usd(
                            dg_min,
                            session.haiku_input_tokens,
                            session.haiku_output_tokens,
                        ),
                    }
            # Check for recoverable running session in DB
            running = (
                db.query(SessionModel)
                .filter(SessionModel.status == SessionStatus.RUNNING.value)
                .order_by(SessionModel.started_at.desc())
                .first()
            )
            if running:
                return {
                    "session_id": running.id,
                    "status": "interrupted",
                    "spec_version": running.spec_version,
                    "replay_mode": running.replay_mode,
                    "deepgram_minutes": running.deepgram_minutes,
                    "haiku_input_tokens": running.haiku_input_tokens,
                    "haiku_output_tokens": running.haiku_output_tokens,
                    "estimated_cost_usd": 0.0,
                }
            return {
                "session_id": None,
                "status": SessionStatus.IDLE.value,
                "spec_version": 0,
                "replay_mode": settings.replay_mode,
                "deepgram_minutes": 0.0,
                "haiku_input_tokens": 0,
                "haiku_output_tokens": 0,
                "estimated_cost_usd": 0.0,
            }
        finally:
            db.close()

    async def start(
        self,
        mic_device_index: Optional[int] = None,
        system_device_index: Optional[int] = None,
        replay_mode: Optional[bool] = None,
        replay_mic_wav: Optional[str] = None,
        replay_system_wav: Optional[str] = None,
    ) -> dict[str, Any]:
        if self._session_id:
            raise RuntimeError("Session already running")

        self._loop = asyncio.get_running_loop()
        self._replay_mode = replay_mode if replay_mode is not None else settings.replay_mode
        mic_wav = Path(replay_mic_wav or settings.replay_mic_wav)
        system_wav = Path(replay_system_wav or settings.replay_system_wav)
        if not mic_wav.is_absolute():
            mic_wav = (BACKEND_DIR / mic_wav).resolve()
        if not system_wav.is_absolute():
            system_wav = (BACKEND_DIR / system_wav).resolve()
        self._replay_mic_path = mic_wav
        self._replay_system_path = system_wav

        session_id = new_session_id()
        db = SessionLocal()
        try:
            session = SessionModel(
                id=session_id,
                status=SessionStatus.RUNNING.value,
                started_at=utcnow(),
                replay_mode=self._replay_mode,
                mic_device_index=mic_device_index,
                system_device_index=system_device_index,
            )
            db.add(session)
            db.commit()
        finally:
            db.close()

        self._session_id = session_id
        self._stop_event.clear()

        await ws_hub.broadcast(
            "session.state",
            {"session_id": session_id, "status": "running", "replay_mode": self._replay_mode},
        )

        if self._replay_mode:
            import wave

            with wave.open(str(mic_wav), "rb") as wf:
                self._sample_rates["mic"] = wf.getframerate()
            with wave.open(str(system_wav), "rb") as wf:
                self._sample_rates["system"] = wf.getframerate()

            self._whisper_mic = WhisperTranscriptionClient(
                "mic", self._sample_rates["mic"], self._on_transcript, self._on_utterance_end
            )
            self._whisper_system = WhisperTranscriptionClient(
                "system", self._sample_rates["system"], self._on_transcript, self._on_utterance_end
            )
            await self._whisper_mic.start()
            await self._whisper_system.start()

            replay_task = asyncio.create_task(
                run_replay_streams(mic_wav, system_wav, self._on_audio_chunk, self._stop_event)
            )
            replay_task.add_done_callback(self._on_replay_finished)
            self._tasks.append(replay_task)
        else:
            devices = list_devices()
            loopbacks = [d for d in devices if d.kind == "loopback"]
            inputs = [d for d in devices if d.kind == "input"]
            if system_device_index is None and loopbacks:
                system_device_index = loopbacks[0].index
            if mic_device_index is None and inputs:
                mic_device_index = inputs[0].index
            if mic_device_index is None or system_device_index is None:
                raise RuntimeError("No audio devices found. Select devices or use replay mode.")

            self._mic_stream = LiveAudioStream(mic_device_index, "mic", self._on_audio_chunk_sync)
            self._system_stream = LiveAudioStream(system_device_index, "system", self._on_audio_chunk_sync)
            self._sample_rates["mic"] = self._mic_stream.sample_rate
            self._sample_rates["system"] = self._system_stream.sample_rate

            self._whisper_mic = WhisperTranscriptionClient(
                "mic", self._sample_rates["mic"], self._on_transcript, self._on_utterance_end
            )
            self._whisper_system = WhisperTranscriptionClient(
                "system", self._sample_rates["system"], self._on_transcript, self._on_utterance_end
            )
            await self._whisper_mic.start()
            await self._whisper_system.start()

            self._mic_stream.start()
            self._system_stream.start()

        self._force_distill_task = asyncio.create_task(self._force_distill_loop())
        self._tasks.append(self._force_distill_task)

        return self.get_state()

    def recover_interrupted_sessions(self) -> None:
        """Mark orphaned in-memory state; DB rows stay running for read recovery."""
        db = SessionLocal()
        try:
            running = (
                db.query(SessionModel)
                .filter(SessionModel.status == SessionStatus.RUNNING.value)
                .order_by(SessionModel.started_at.desc())
                .all()
            )
            for session in running:
                logger.warning(
                    "Recovered interrupted session %s (spec_v=%s) — read-only until stopped",
                    session.id,
                    session.spec_version,
                )
        finally:
            db.close()

    async def stop(self) -> dict[str, Any]:
        session_id = self._session_id
        if not session_id:
            db = SessionLocal()
            try:
                interrupted = (
                    db.query(SessionModel)
                    .filter(SessionModel.status == SessionStatus.RUNNING.value)
                    .order_by(SessionModel.started_at.desc())
                    .first()
                )
                if interrupted:
                    session_id = interrupted.id
            finally:
                db.close()
            if not session_id:
                return self.get_state()

        had_live_audio = self._session_id is not None
        self._stop_event.set()

        if self._mic_stream:
            self._mic_stream.stop()
            self._mic_stream = None
        if self._system_stream:
            self._system_stream.stop()
            self._system_stream = None

        dg_mic_min = 0.0
        dg_sys_min = 0.0
        if self._whisper_mic:
            await self._whisper_mic.stop()
            dg_mic_min = self._whisper_mic.minutes_streamed
            self._whisper_mic = None
        if self._whisper_system:
            await self._whisper_system.stop()
            dg_sys_min = self._whisper_system.minutes_streamed
            self._whisper_system = None

        if had_live_audio:
            for task in self._tasks:
                task.cancel()
            self._tasks.clear()

        session_id = self._session_id or session_id
        db = SessionLocal()
        try:
            session = db.get(SessionModel, session_id)
            if session:
                session.status = SessionStatus.IDLE.value
                session.stopped_at = utcnow()
                if had_live_audio:
                    session.deepgram_minutes += dg_mic_min + dg_sys_min
                db.commit()
        finally:
            db.close()

        self._session_id = None
        state = self.get_state()
        await ws_hub.broadcast("session.state", state)
        return state

    def _on_audio_chunk_sync(self, chunk: bytes, sample_rate: float, channel: str) -> None:
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._on_audio_chunk(chunk, sample_rate, channel), self._loop
            )

    def _on_audio_chunk(self, chunk: bytes, sample_rate: float, channel: str) -> None:
        self._sample_rates[channel] = int(sample_rate)
        if channel == "mic" and self._whisper_mic:
            asyncio.create_task(self._whisper_mic.send_audio(chunk))
        elif channel == "system" and self._whisper_system:
            asyncio.create_task(self._whisper_system.send_audio(chunk))

    async def _on_transcript(self, channel: str, text: str, is_final: bool) -> None:
        if not self._session_id:
            return
        text = text.strip()
        if not text:
            return

        speaker = "me" if channel == "mic" else "remote"
        now = utcnow()

        if is_final:
            db = SessionLocal()
            try:
                seg = TranscriptSegmentModel(
                    session_id=self._session_id,
                    ts=now,
                    channel=channel,
                    speaker=speaker,
                    text=text,
                    is_final=True,
                    distilled=False,
                )
                db.add(seg)
                db.commit()
                db.refresh(seg)
                payload = {
                    "id": seg.id,
                    "ts": seg.ts.isoformat(),
                    "channel": channel,
                    "speaker": speaker,
                    "text": text,
                    "is_final": True,
                }
            finally:
                db.close()
            await ws_hub.broadcast("transcript.append", payload)
        else:
            await ws_hub.broadcast(
                "transcript.append",
                {
                    "id": -1,
                    "ts": now.isoformat(),
                    "channel": channel,
                    "speaker": speaker,
                    "text": text,
                    "is_final": False,
                },
            )

    async def _on_utterance_end(self, channel: str) -> None:
        if self._replay_mode:
            return
        if self._session_id:
            await distiller.maybe_distill(self._session_id, trigger=f"utterance_end:{channel}")

    async def _force_distill_loop(self) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(5)
            if self._session_id:
                await distiller.force_if_stale(self._session_id)

    def _on_replay_finished(self, task: asyncio.Task) -> None:
        if self._loop and self._session_id and not self._stop_event.is_set():
            asyncio.run_coroutine_threadsafe(self._after_replay(), self._loop)

    async def _after_replay(self) -> None:
        from app.services.whisper_transcription import transcribe_wav_file

        loop = asyncio.get_running_loop()
        # Replace partial VAD segments with full-file transcription for replay accuracy
        if self._session_id and self._replay_mic_path and self._replay_system_path:
            db = SessionLocal()
            try:
                db.query(TranscriptSegmentModel).filter(
                    TranscriptSegmentModel.session_id == self._session_id
                ).delete()
                db.commit()
            finally:
                db.close()

            for path, channel in (
                (self._replay_mic_path, "mic"),
                (self._replay_system_path, "system"),
            ):
                text = await loop.run_in_executor(None, transcribe_wav_file, path)
                if text:
                    await self._on_transcript(channel, text, True)

        if self._whisper_mic:
            await self._whisper_mic.flush()
        if self._whisper_system:
            await self._whisper_system.flush()
        if self._session_id:
            await distiller.maybe_distill(self._session_id, trigger="replay_end", force=True)
            # Second distill after spacing to bump spec version for journey tab
            await asyncio.sleep(settings.distill_min_interval_sec + 1)
            await distiller.maybe_distill(self._session_id, trigger="replay_followup", force=True)


session_manager = SessionManager()
