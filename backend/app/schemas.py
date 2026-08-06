"""Pydantic schemas for API and websocket payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class DeviceInfo(BaseModel):
    index: int
    name: str
    kind: Literal["input", "loopback"]
    default_sample_rate: float
    max_input_channels: int


class SessionStartRequest(BaseModel):
    mic_device_index: Optional[int] = None
    system_device_index: Optional[int] = None
    replay_mode: Optional[bool] = None
    replay_mic_wav: Optional[str] = None
    replay_system_wav: Optional[str] = None


class TranscriptSegmentOut(BaseModel):
    id: int
    ts: datetime
    channel: str
    speaker: str
    text: str
    is_final: bool


class SpecItemOut(BaseModel):
    uuid: str
    requirement: str
    status: str
    evidence_quote: str
    category: str
    acceptance_hint: Optional[str] = None
    built_at_version: Optional[int] = None
    supersedes: Optional[str] = None
    locked_by_human: bool
    spec_version: int


class SpecChangeOut(BaseModel):
    id: int
    spec_version: int
    item_uuid: str
    action: str
    reason: str
    ts: datetime


class SpecOverrideRequest(BaseModel):
    status: Literal["confirmed", "tentative", "retracted"]
    unlock: bool = False


class SessionStateOut(BaseModel):
    session_id: Optional[str] = None
    status: str
    spec_version: int
    replay_mode: bool
    deepgram_minutes: float
    haiku_input_tokens: int
    haiku_output_tokens: int
    estimated_cost_usd: float


class SpecResponse(BaseModel):
    version: int
    items: list[SpecItemOut]


class TranscriptResponse(BaseModel):
    segments: list[TranscriptSegmentOut]


class DistillChange(BaseModel):
    id: str
    action: Literal["add", "update", "retract"]
    reason: str


class DistillOutput(BaseModel):
    changes: list[DistillChange]
    spec: list[dict] = Field(default_factory=list)


class WSEvent(BaseModel):
    type: str
    payload: dict


class PathwayOut(BaseModel):
    id: str
    title: str
    summary: str
    effort: str
    tradeoffs: str
    approach: str


class ViabilityOut(BaseModel):
    status: Literal["green", "amber", "red"]
    summary: str
    constraints: list[str] = Field(default_factory=list)


class BriefOut(BaseModel):
    id: str
    session_id: str
    spec_version: int
    goal: str
    summary: str
    actionable_items: list[dict]
    deferred_items: list[dict]
    viability: ViabilityOut
    pathways: list[PathwayOut]
    recommended_pathway_id: str
    selected_pathway_id: Optional[str] = None
    created_at: datetime


class PathwaySelectRequest(BaseModel):
    pathway_id: str


class BuildRunOut(BaseModel):
    id: int
    session_id: str
    spec_version: int
    brief_id: Optional[str] = None
    pathway_id: Optional[str] = None
    status: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    files_changed: list[str] = Field(default_factory=list)
    agent_summary: str = ""
    duration_sec: float = 0.0
    cost_usd: float = 0.0
    test_status: str = "pending"
    test_log: str = ""
    push_status: str = "pending"
    repo_url: str = ""
    error: str = ""
