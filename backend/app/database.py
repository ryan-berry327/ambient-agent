"""SQLAlchemy models and database setup."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


class SessionStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"


class SpecStatus(str, Enum):
    CONFIRMED = "confirmed"
    TENTATIVE = "tentative"
    RETRACTED = "retracted"


class SpecAction(str, Enum):
    ADD = "add"
    UPDATE = "update"
    RETRACT = "retract"


class SessionModel(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default=SessionStatus.IDLE.value)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    spec_version: Mapped[int] = mapped_column(Integer, default=0)
    replay_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    mic_device_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    system_device_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deepgram_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    haiku_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    haiku_output_tokens: Mapped[int] = mapped_column(Integer, default=0)

    transcript_segments: Mapped[list["TranscriptSegmentModel"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    spec_items: Mapped[list["SpecItemModel"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    spec_changes: Mapped[list["SpecChangeModel"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    distill_runs: Mapped[list["DistillRunModel"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class TranscriptSegmentModel(Base):
    __tablename__ = "transcript_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    channel: Mapped[str] = mapped_column(String(10))  # mic | system
    speaker: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    is_final: Mapped[bool] = mapped_column(Boolean, default=True)
    distilled: Mapped[bool] = mapped_column(Boolean, default=False)

    session: Mapped["SessionModel"] = relationship(back_populates="transcript_segments")


class SpecItemModel(Base):
    __tablename__ = "spec_items"

    uuid: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), index=True)
    requirement: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    evidence_quote: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64), default="general")
    acceptance_hint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    built_at_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    supersedes: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    locked_by_human: Mapped[bool] = mapped_column(Boolean, default=False)
    spec_version: Mapped[int] = mapped_column(Integer, default=0)

    session: Mapped["SessionModel"] = relationship(back_populates="spec_items")


class SpecChangeModel(Base):
    __tablename__ = "spec_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), index=True)
    spec_version: Mapped[int] = mapped_column(Integer)
    item_uuid: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    session: Mapped["SessionModel"] = relationship(back_populates="spec_changes")


class DistillRunModel(Base):
    __tablename__ = "distill_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), index=True)
    spec_version: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    trigger: Mapped[str] = mapped_column(String(32), default="manual")

    session: Mapped["SessionModel"] = relationship(back_populates="distill_runs")


class BuildRunModel(Base):
    """Placeholder for Phase B."""

    __tablename__ = "build_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), index=True)
    spec_version: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    files_changed: Mapped[str] = mapped_column(Text, default="[]")
    agent_summary: Mapped[str] = mapped_column(Text, default="")
    duration_sec: Mapped[float] = mapped_column(Float, default=0.0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_session_id() -> str:
    return str(uuid.uuid4())


settings.database_path.parent.mkdir(parents=True, exist_ok=True)
settings.specs_dir.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{settings.database_path}",
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
