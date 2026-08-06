"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.config import settings
from app.database import (
    SessionLocal,
    SessionModel,
    SpecChangeModel,
    SpecItemModel,
    SpecStatus,
    TranscriptSegmentModel,
    get_db,
    init_db,
)
from app.schemas import (
    BriefOut,
    BuildRunOut,
    DeviceInfo,
    PathwaySelectRequest,
    SessionStartRequest,
    SessionStateOut,
    SpecChangeOut,
    SpecItemOut,
    SpecOverrideRequest,
    SpecResponse,
    TranscriptResponse,
    TranscriptSegmentOut,
)
from app.services.audio_devices import list_devices
from app.services.brief_service import brief_service
from app.services.builder_service import builder_service
from app.services.cost_tracker import estimate_cost_usd
from app.services.session_manager import session_manager
from app.services.ws_hub import ws_hub

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Ambient Call Agent", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    session_manager.recover_interrupted_sessions()
    logger.info("Database initialized at %s", settings.database_path)


def _active_session_id(db: Session) -> Optional[str]:
    sid = session_manager.active_session_id
    if sid:
        return sid
    running = (
        db.query(SessionModel)
        .filter(SessionModel.status == "running")
        .order_by(SessionModel.started_at.desc())
        .first()
    )
    if running:
        return running.id
    # Allow brief/build after the call ends
    latest = db.query(SessionModel).order_by(SessionModel.started_at.desc()).first()
    return latest.id if latest else None


@app.get("/devices", response_model=list[DeviceInfo])
def get_devices() -> list[DeviceInfo]:
    return list_devices()


@app.post("/session/start", response_model=SessionStateOut)
async def start_session(body: SessionStartRequest) -> SessionStateOut:
    try:
        state = await session_manager.start(
            mic_device_index=body.mic_device_index,
            system_device_index=body.system_device_index,
            replay_mode=body.replay_mode,
            replay_mic_wav=body.replay_mic_wav,
            replay_system_wav=body.replay_system_wav,
        )
        return SessionStateOut(**state)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/session/stop", response_model=SessionStateOut)
async def stop_session() -> SessionStateOut:
    state = await session_manager.stop()
    return SessionStateOut(**state)


@app.get("/session/state", response_model=SessionStateOut)
def session_state() -> SessionStateOut:
    return SessionStateOut(**session_manager.get_state())


@app.get("/transcript", response_model=TranscriptResponse)
def get_transcript(db: Session = Depends(get_db)) -> TranscriptResponse:
    sid = _active_session_id(db)
    if not sid:
        return TranscriptResponse(segments=[])
    rows = (
        db.query(TranscriptSegmentModel)
        .filter(TranscriptSegmentModel.session_id == sid, TranscriptSegmentModel.is_final == True)  # noqa: E712
        .order_by(TranscriptSegmentModel.ts.asc())
        .all()
    )
    return TranscriptResponse(
        segments=[
            TranscriptSegmentOut(
                id=r.id,
                ts=r.ts,
                channel=r.channel,
                speaker=r.speaker,
                text=r.text,
                is_final=r.is_final,
            )
            for r in rows
        ]
    )


@app.get("/spec", response_model=SpecResponse)
def get_spec(db: Session = Depends(get_db)) -> SpecResponse:
    sid = _active_session_id(db)
    if not sid:
        return SpecResponse(version=0, items=[])
    session = db.get(SessionModel, sid)
    items = (
        db.query(SpecItemModel)
        .filter(SpecItemModel.session_id == sid)
        .order_by(SpecItemModel.spec_version.desc())
        .all()
    )
    return SpecResponse(
        version=session.spec_version if session else 0,
        items=[
            SpecItemOut(
                uuid=i.uuid,
                requirement=i.requirement,
                status=i.status,
                evidence_quote=i.evidence_quote,
                category=i.category,
                acceptance_hint=i.acceptance_hint,
                built_at_version=i.built_at_version,
                supersedes=i.supersedes,
                locked_by_human=i.locked_by_human,
                spec_version=i.spec_version,
            )
            for i in items
        ],
    )


@app.get("/spec/changes", response_model=list[SpecChangeOut])
def get_spec_changes(db: Session = Depends(get_db)) -> list[SpecChangeOut]:
    sid = _active_session_id(db)
    if not sid:
        return []
    rows = (
        db.query(SpecChangeModel)
        .filter(SpecChangeModel.session_id == sid)
        .order_by(SpecChangeModel.spec_version.asc(), SpecChangeModel.ts.asc())
        .all()
    )
    return [
        SpecChangeOut(
            id=r.id,
            spec_version=r.spec_version,
            item_uuid=r.item_uuid,
            action=r.action,
            reason=r.reason,
            ts=r.ts,
        )
        for r in rows
    ]


@app.get("/brief", response_model=Optional[BriefOut])
def get_brief(db: Session = Depends(get_db)) -> Optional[BriefOut]:
    sid = _active_session_id(db)
    if not sid:
        # Fall back to most recent session with a brief
        from app.database import BriefModel

        row = db.query(BriefModel).order_by(BriefModel.created_at.desc()).first()
        if not row:
            return None
        return brief_service.get_brief(row.session_id)
    return brief_service.get_brief(sid)


@app.post("/brief/generate", response_model=BriefOut)
async def generate_brief(db: Session = Depends(get_db)) -> BriefOut:
    sid = _active_session_id(db)
    if not sid:
        raise HTTPException(status_code=404, detail="No active session")
    try:
        return await brief_service.generate(sid, use_cursor=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/brief/select-pathway", response_model=BriefOut)
async def select_pathway(body: PathwaySelectRequest, db: Session = Depends(get_db)) -> BriefOut:
    sid = _active_session_id(db)
    if not sid:
        raise HTTPException(status_code=404, detail="No active session")
    try:
        return await brief_service.select_pathway_async(sid, body.pathway_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/build/start", response_model=BuildRunOut)
async def start_build(db: Session = Depends(get_db)) -> BuildRunOut:
    sid = _active_session_id(db)
    if not sid:
        raise HTTPException(status_code=404, detail="No active session")
    try:
        return await builder_service.start_build(sid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/build/latest", response_model=Optional[BuildRunOut])
def get_latest_build(db: Session = Depends(get_db)) -> Optional[BuildRunOut]:
    sid = _active_session_id(db)
    if not sid:
        from app.database import BuildRunModel

        row = db.query(BuildRunModel).order_by(BuildRunModel.id.desc()).first()
        if not row:
            return None
        return builder_service.get_latest(row.session_id)
    return builder_service.get_latest(sid)


@app.post("/spec/{item_uuid}/override", response_model=SpecItemOut)
async def override_spec(item_uuid: str, body: SpecOverrideRequest, db: Session = Depends(get_db)) -> SpecItemOut:
    sid = _active_session_id(db)
    if not sid:
        raise HTTPException(status_code=404, detail="No active session")

    item = (
        db.query(SpecItemModel)
        .filter(SpecItemModel.session_id == sid, SpecItemModel.uuid == item_uuid)
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Spec item not found")

    if body.unlock:
        item.locked_by_human = False
    else:
        item.status = body.status
        item.locked_by_human = True
    db.commit()
    db.refresh(item)

    out = SpecItemOut(
        uuid=item.uuid,
        requirement=item.requirement,
        status=item.status,
        evidence_quote=item.evidence_quote,
        category=item.category,
        acceptance_hint=item.acceptance_hint,
        built_at_version=item.built_at_version,
        supersedes=item.supersedes,
        locked_by_human=item.locked_by_human,
        spec_version=item.spec_version,
    )
    await ws_hub.broadcast("spec.updated", {"version": item.spec_version, "items": [out.model_dump()]})
    return out


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await ws_hub.connect(websocket)
    try:
        # Send current state on connect
        state = session_manager.get_state()
        await websocket.send_json({"type": "session.state", "payload": state})

        db = SessionLocal()
        try:
            sid = _active_session_id(db)
            if sid:
                segments = (
                    db.query(TranscriptSegmentModel)
                    .filter(
                        TranscriptSegmentModel.session_id == sid,
                        TranscriptSegmentModel.is_final == True,  # noqa: E712
                    )
                    .order_by(TranscriptSegmentModel.ts.asc())
                    .all()
                )
                for seg in segments:
                    await websocket.send_json(
                        {
                            "type": "transcript.append",
                            "payload": {
                                "id": seg.id,
                                "ts": seg.ts.isoformat(),
                                "channel": seg.channel,
                                "speaker": seg.speaker,
                                "text": seg.text,
                                "is_final": True,
                            },
                        }
                    )
                spec = get_spec(db)
                if spec.items:
                    await websocket.send_json(
                        {
                            "type": "spec.updated",
                            "payload": {
                                "version": spec.version,
                                "items": [i.model_dump() for i in spec.items],
                            },
                        }
                    )
                brief = brief_service.get_brief(sid)
                if brief:
                    await websocket.send_json(
                        {
                            "type": "brief.updated",
                            "payload": brief.model_dump(mode="json"),
                        }
                    )
                build = builder_service.get_latest(sid)
                if build:
                    await websocket.send_json(
                        {
                            "type": "build.updated",
                            "payload": build.model_dump(mode="json"),
                        }
                    )
        finally:
            db.close()

        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await ws_hub.disconnect(websocket)
