"""Application configuration loaded from environment."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    cursor_api_key: str = ""
    cursor_model: str = "composer-2.5"
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    replay_mode: bool = True
    replay_mic_wav: str = str(PROJECT_ROOT / "samples" / "mic.wav")
    replay_system_wav: str = str(PROJECT_ROOT / "samples" / "system.wav")
    distill_min_interval_sec: float = 10.0
    distill_force_interval_sec: float = 45.0
    whisper_model: str = "base"
    whisper_device: str = "cpu"
    whisper_vad_rms_threshold: float = 150.0
    # Future: set to "deepgram" for live streaming STT upgrade
    transcription_backend: str = "whisper"

    database_path: Path = BACKEND_DIR / "data" / "ambient.db"
    specs_dir: Path = BACKEND_DIR / "specs"


settings = Settings()
