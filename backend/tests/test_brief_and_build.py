"""Phase B: brief heuristic + builder scaffold/smoke (no Cursor API required)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.database import (
    BriefModel,
    SessionLocal,
    SessionModel,
    SpecItemModel,
    SpecStatus,
    init_db,
    new_session_id,
    utcnow,
)
from app.services.brief_service import BriefService
from app.services.builder_service import BuilderService


@pytest.fixture()
def db_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(settings, "database_path", db_path)
    monkeypatch.setattr(settings, "briefs_dir", tmp_path / "briefs")
    monkeypatch.setattr(settings, "builds_dir", tmp_path / "builds")
    monkeypatch.setattr(settings, "specs_dir", tmp_path / "specs")
    settings.briefs_dir.mkdir(parents=True, exist_ok=True)
    settings.builds_dir.mkdir(parents=True, exist_ok=True)
    settings.specs_dir.mkdir(parents=True, exist_ok=True)

    # Rebind engine to temp DB
    from app import database as database_mod
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    database_mod.engine = engine
    database_mod.SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    database_mod.Base.metadata.create_all(bind=engine)

    sid = new_session_id()
    db = database_mod.SessionLocal()
    db.add(
        SessionModel(
            id=sid,
            status="running",
            started_at=utcnow(),
            spec_version=1,
            replay_mode=True,
        )
    )
    items = [
        SpecItemModel(
            uuid=str(uuid.uuid4()),
            session_id=sid,
            requirement="Real-time sales dashboard",
            status=SpecStatus.CONFIRMED.value,
            evidence_quote="shows our sales numbers in real time",
            category="ui",
            acceptance_hint="Chart renders dummy revenue series",
            spec_version=1,
        ),
        SpecItemModel(
            uuid=str(uuid.uuid4()),
            session_id=sid,
            requirement="Login page",
            status=SpecStatus.CONFIRMED.value,
            evidence_quote="need a login page",
            category="auth",
            acceptance_hint="Mock auth accepts demo user",
            spec_version=1,
        ),
        SpecItemModel(
            uuid=str(uuid.uuid4()),
            session_id=sid,
            requirement="Export to CSV",
            status=SpecStatus.TENTATIVE.value,
            evidence_quote="export to CSV",
            category="api",
            acceptance_hint="CSV includes month,revenue",
            spec_version=1,
        ),
        SpecItemModel(
            uuid=str(uuid.uuid4()),
            session_id=sid,
            requirement="Mobile support",
            status=SpecStatus.RETRACTED.value,
            evidence_quote="skip mobile for now",
            category="ui",
            spec_version=1,
        ),
    ]
    for item in items:
        db.add(item)
    db.commit()
    db.close()
    return sid


def test_heuristic_brief_viability_and_pathways(db_session: str):
    svc = BriefService()
    brief = svc._generate_sync(db_session, use_cursor=False)
    assert brief.goal
    assert brief.viability.status in {"green", "amber", "red"}
    assert brief.viability.status == "green"
    assert len(brief.pathways) >= 2
    assert brief.selected_pathway_id == brief.recommended_pathway_id
    assert len(brief.actionable_items) == 3
    assert len(brief.deferred_items) == 1
    assert any(c for c in brief.viability.constraints)


def test_select_pathway_and_build_smoke(db_session: str):
    brief_svc = BriefService()
    brief = brief_svc._generate_sync(db_session, use_cursor=False)
    out = brief_svc._select_pathway_sync(db_session, "mvp")
    assert out.selected_pathway_id == "mvp"

    builder = BuilderService()
    run = builder._execute_build(builder._create_run(db_session))
    assert run.test_status == "passed"
    assert run.status == "succeeded"
    assert run.push_status == "local_only"
    assert run.pathway_id == "mvp"
    assert Path(run.repo_url).exists()
    smoke = Path(run.repo_url) / "scripts" / "smoke_dummy.mjs"
    assert smoke.exists()
    dummy = json.loads((Path(run.repo_url) / "src" / "data" / "dummy.json").read_text())
    assert dummy["features"]
    assert "Login" in " ".join(f["title"] for f in dummy["features"]) or any(
        f["category"] == "auth" for f in dummy["features"]
    )


def test_red_viability_blocks_build(db_session: str, monkeypatch: pytest.MonkeyPatch):
    brief_svc = BriefService()
    brief_svc._generate_sync(db_session, use_cursor=False)

    # Force red viability
    from app import database as database_mod

    db = database_mod.SessionLocal()
    row = db.query(BriefModel).filter(BriefModel.session_id == db_session).one()
    row.viability_json = json.dumps(
        {"status": "red", "summary": "blocked", "constraints": ["no scope"]}
    )
    db.commit()
    db.close()

    builder = BuilderService()
    with pytest.raises(ValueError, match="red"):
        builder._create_run(db_session)
