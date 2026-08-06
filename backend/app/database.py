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
    briefs: Mapped[list["BriefModel"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    build_runs: Mapped[list["BuildRunModel"]] = relationship(
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


class BriefModel(Base):
    __tablename__ = "briefs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), index=True)
    spec_version: Mapped[int] = mapped_column(Integer, default=0)
    goal: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    actionable_json: Mapped[str] = mapped_column(Text, default="[]")
    deferred_json: Mapped[str] = mapped_column(Text, default="[]")
    viability_json: Mapped[str] = mapped_column(Text, default="{}")
    pathways_json: Mapped[str] = mapped_column(Text, default="[]")
    recommended_pathway_id: Mapped[str] = mapped_column(String(64), default="exact")
    selected_pathway_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    session: Mapped["SessionModel"] = relationship(back_populates="briefs")


class BuildRunModel(Base):
    __tablename__ = "build_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), index=True)
    spec_version: Mapped[int] = mapped_column(Integer)
    brief_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    pathway_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    files_changed: Mapped[str] = mapped_column(Text, default="[]")
    agent_summary: Mapped[str] = mapped_column(Text, default="")
    duration_sec: Mapped[float] = mapped_column(Float, default=0.0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    test_status: Mapped[str] = mapped_column(String(32), default="pending")
    test_log: Mapped[str] = mapped_column(Text, default="")
    push_status: Mapped[str] = mapped_column(String(32), default="pending")
    repo_url: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")

    session: Mapped["SessionModel"] = relationship(back_populates="build_runs")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_session_id() -> str:
    return str(uuid.uuid4())


settings.database_path.parent.mkdir(parents=True, exist_ok=True)
settings.specs_dir.mkdir(parents=True, exist_ok=True)
settings.briefs_dir.mkdir(parents=True, exist_ok=True)
settings.builds_dir.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{settings.database_path}",
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _migrate_sqlite() -> None:
    """Add Phase B columns to older ambient.db files."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "build_runs" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("build_runs")}
    alters = {
        "brief_id": "ALTER TABLE build_runs ADD COLUMN brief_id VARCHAR(36)",
        "pathway_id": "ALTER TABLE build_runs ADD COLUMN pathway_id VARCHAR(64)",
        "status": "ALTER TABLE build_runs ADD COLUMN status VARCHAR(32) DEFAULT 'queued'",
        "test_status": "ALTER TABLE build_runs ADD COLUMN test_status VARCHAR(32) DEFAULT 'pending'",
        "test_log": "ALTER TABLE build_runs ADD COLUMN test_log TEXT DEFAULT ''",
        "push_status": "ALTER TABLE build_runs ADD COLUMN push_status VARCHAR(32) DEFAULT 'pending'",
        "repo_url": "ALTER TABLE build_runs ADD COLUMN repo_url TEXT DEFAULT ''",
        "error": "ALTER TABLE build_runs ADD COLUMN error TEXT DEFAULT ''",
    }
    with engine.begin() as conn:
        for name, sql in alters.items():
            if name not in existing:
                conn.execute(text(sql))


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_sqlite()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
