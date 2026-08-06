"""Brief + viability + pathway review from distilled spec."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any, Optional

from app.config import settings
from app import database
from app.database import BriefModel, SessionModel, SpecItemModel, SpecStatus, utcnow
from app.schemas import BriefOut, PathwayOut, ViabilityOut
from app.services.cursor_api import CursorApiError, cursor_client
from app.services.ws_hub import ws_hub

logger = logging.getLogger(__name__)

BRIEF_PROMPT = """You are a product/engineering reviewer for an ambient call agent.

Given a distilled requirements spec from a live conversation, produce a build brief
with viability assessment and alternative pathways as strict JSON.

Output format (JSON only, no markdown fences):
{
  "goal": "<one-sentence product goal>",
  "summary": "<2-3 sentence brief of what was asked>",
  "actionable_ids": ["<uuid of items that should drive the build>"],
  "deferred_ids": ["<uuid of items to defer or ignore>"],
  "viability": {
    "status": "green|amber|red",
    "summary": "<can we build this with current info?>",
    "constraints": ["<constraint or missing dependency>"]
  },
  "pathways": [
    {
      "id": "exact",
      "title": "<short title>",
      "summary": "<what this path delivers>",
      "effort": "low|medium|high",
      "tradeoffs": "<tradeoffs>",
      "approach": "<how to implement>"
    }
  ],
  "recommended_pathway_id": "exact"
}

Rules:
- Prefer 2-3 pathways: e.g. exact ask, lean MVP, more efficient alternative.
- actionable_ids should be confirmed items plus strong tentative ones; exclude retracted.
- viability green = enough confirmed/clear scope; amber = buildable but gaps; red = not enough to build.
- Be concrete about constraints (auth, data source, integrations, mobile skip, etc.).
- Return ONLY valid JSON."""


class BriefService:
    def __init__(self) -> None:
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def get_brief(self, session_id: str) -> Optional[BriefOut]:
        db = database.SessionLocal()
        try:
            row = (
                db.query(BriefModel)
                .filter(BriefModel.session_id == session_id)
                .order_by(BriefModel.created_at.desc())
                .first()
            )
            if not row:
                return None
            return self._row_to_out(row)
        finally:
            db.close()

    async def generate(self, session_id: str, use_cursor: bool = True) -> BriefOut:
        if self._running:
            raise RuntimeError("Brief generation already in progress")
        self._running = True
        await ws_hub.broadcast("brief.started", {"session_id": session_id})
        try:
            brief = await asyncio.to_thread(self._generate_sync, session_id, use_cursor)
            await ws_hub.broadcast("brief.updated", brief.model_dump(mode="json"))
            return brief
        except Exception as exc:
            logger.exception("Brief generation failed: %s", exc)
            await ws_hub.broadcast("brief.finished", {"session_id": session_id, "error": str(exc)})
            raise
        finally:
            self._running = False
            await ws_hub.broadcast("brief.finished", {"session_id": session_id})

    async def select_pathway_async(self, session_id: str, pathway_id: str) -> BriefOut:
        out = await asyncio.to_thread(self._select_pathway_sync, session_id, pathway_id)
        await ws_hub.broadcast("brief.updated", out.model_dump(mode="json"))
        return out

    def _select_pathway_sync(self, session_id: str, pathway_id: str) -> BriefOut:
        db = database.SessionLocal()
        try:
            row = (
                db.query(BriefModel)
                .filter(BriefModel.session_id == session_id)
                .order_by(BriefModel.created_at.desc())
                .first()
            )
            if not row:
                raise LookupError("No brief for session")
            pathways = json.loads(row.pathways_json)
            ids = {p.get("id") for p in pathways}
            if pathway_id not in ids:
                raise ValueError(f"Unknown pathway_id={pathway_id}")
            row.selected_pathway_id = pathway_id
            db.commit()
            db.refresh(row)
            return self._row_to_out(row)
        finally:
            db.close()

    def _generate_sync(self, session_id: str, use_cursor: bool) -> BriefOut:
        db = database.SessionLocal()
        try:
            session = db.get(SessionModel, session_id)
            if not session:
                raise LookupError("Session not found")

            items = (
                db.query(SpecItemModel)
                .filter(SpecItemModel.session_id == session_id)
                .all()
            )
            if not items:
                raise ValueError("No spec items to brief yet")

            spec_payload = [
                {
                    "id": i.uuid,
                    "requirement": i.requirement,
                    "status": i.status,
                    "evidence_quote": i.evidence_quote,
                    "category": i.category,
                    "acceptance_hint": i.acceptance_hint,
                }
                for i in items
            ]

            parsed: Optional[dict[str, Any]] = None
            if use_cursor and settings.cursor_api_key:
                try:
                    raw = cursor_client.prompt(
                        f"{BRIEF_PROMPT}\n\n## Spec (v{session.spec_version})\n"
                        f"{json.dumps(spec_payload, indent=2)}"
                    )
                    parsed = self._parse_json(raw)
                except (CursorApiError, ValueError) as exc:
                    logger.warning("Cursor brief failed, using heuristic: %s", exc)

            if not parsed:
                parsed = self._heuristic_brief(spec_payload)

            actionable_ids = set(parsed.get("actionable_ids") or [])
            deferred_ids = set(parsed.get("deferred_ids") or [])
            by_id = {i["id"]: i for i in spec_payload}

            # Fall back if model omitted ids
            if not actionable_ids:
                actionable_ids = {
                    i["id"]
                    for i in spec_payload
                    if i["status"] in (SpecStatus.CONFIRMED.value, SpecStatus.TENTATIVE.value)
                }
            if not deferred_ids:
                deferred_ids = {
                    i["id"] for i in spec_payload if i["status"] == SpecStatus.RETRACTED.value
                }

            actionable = [by_id[i] for i in actionable_ids if i in by_id]
            deferred = [by_id[i] for i in deferred_ids if i in by_id]

            pathways_raw = parsed.get("pathways") or []
            pathways = [self._normalize_pathway(p, idx) for idx, p in enumerate(pathways_raw)]
            if not pathways:
                pathways = self._default_pathways(actionable)

            recommended = parsed.get("recommended_pathway_id") or pathways[0]["id"]
            if recommended not in {p["id"] for p in pathways}:
                recommended = pathways[0]["id"]

            viability_raw = parsed.get("viability") or {}
            viability = {
                "status": viability_raw.get("status")
                or self._heuristic_viability_status(actionable),
                "summary": viability_raw.get("summary")
                or "Scope assessed from distilled requirements.",
                "constraints": viability_raw.get("constraints") or [],
            }

            # Replace any prior brief for this session (one active brief)
            db.query(BriefModel).filter(BriefModel.session_id == session_id).delete()
            row = BriefModel(
                id=str(uuid.uuid4()),
                session_id=session_id,
                spec_version=session.spec_version,
                goal=str(parsed.get("goal") or self._default_goal(actionable)),
                summary=str(parsed.get("summary") or "Build brief generated from call requirements."),
                actionable_json=json.dumps(actionable),
                deferred_json=json.dumps(deferred),
                viability_json=json.dumps(viability),
                pathways_json=json.dumps(pathways),
                recommended_pathway_id=recommended,
                selected_pathway_id=recommended,
                created_at=utcnow(),
            )
            db.add(row)

            # Token accounting (rough)
            approx_in = max(len(json.dumps(spec_payload)) // 4, 1)
            approx_out = max(len(json.dumps(parsed)) // 4, 1)
            session.haiku_input_tokens += approx_in
            session.haiku_output_tokens += approx_out
            db.commit()
            db.refresh(row)

            snapshot = {
                "id": row.id,
                "goal": row.goal,
                "summary": row.summary,
                "viability": viability,
                "pathways": pathways,
                "recommended_pathway_id": recommended,
            }
            path = settings.briefs_dir / f"brief_{session_id[:8]}_v{session.spec_version}.json"
            path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

            return self._row_to_out(row)
        finally:
            db.close()

    def _row_to_out(self, row: BriefModel) -> BriefOut:
        viability = ViabilityOut.model_validate(json.loads(row.viability_json))
        pathways = [PathwayOut.model_validate(p) for p in json.loads(row.pathways_json)]
        return BriefOut(
            id=row.id,
            session_id=row.session_id,
            spec_version=row.spec_version,
            goal=row.goal,
            summary=row.summary,
            actionable_items=json.loads(row.actionable_json),
            deferred_items=json.loads(row.deferred_json),
            viability=viability,
            pathways=pathways,
            recommended_pathway_id=row.recommended_pathway_id,
            selected_pathway_id=row.selected_pathway_id,
            created_at=row.created_at,
        )

    def _parse_json(self, raw: str) -> dict[str, Any]:
        cleaned = raw.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = re.sub(r"```(?:json)?", "", cleaned).strip()
        if not cleaned.startswith("{"):
            match = re.search(r"\{[\s\S]*\}", cleaned)
            if match:
                cleaned = match.group(0)
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError("Brief JSON root must be object")
        return data

    def _heuristic_brief(self, spec_payload: list[dict[str, Any]]) -> dict[str, Any]:
        actionable = [
            i
            for i in spec_payload
            if i["status"] in (SpecStatus.CONFIRMED.value, SpecStatus.TENTATIVE.value)
        ]
        deferred = [i for i in spec_payload if i["status"] == SpecStatus.RETRACTED.value]
        confirmed = [i for i in actionable if i["status"] == SpecStatus.CONFIRMED.value]
        goal = self._default_goal(actionable)
        constraints: list[str] = []
        cats = {i.get("category", "general") for i in actionable}
        if "auth" in cats:
            constraints.append("Authentication approach not fully specified — default to simple login mock.")
        if "data" in cats:
            constraints.append("Live data source unknown — use dummy/fixture data for first build.")
        skip_mobile = any("mobile" in (i.get("requirement") or "").lower() for i in deferred)
        if skip_mobile or any("skip mobile" in (i.get("requirement") or "").lower() for i in actionable):
            constraints.append("Mobile out of scope for v1.")
        if not confirmed:
            constraints.append("No confirmed requirements yet — pathways rely on tentative items.")

        pathways = self._default_pathways(actionable)
        # Prefer MVP if nothing confirmed
        recommended = "mvp" if not confirmed and any(p["id"] == "mvp" for p in pathways) else "exact"

        return {
            "goal": goal,
            "summary": (
                f"Call distilled into {len(actionable)} actionable requirement(s) "
                f"({len(confirmed)} confirmed). "
                f"{len(deferred)} item(s) deferred/retracted."
            ),
            "actionable_ids": [i["id"] for i in actionable],
            "deferred_ids": [i["id"] for i in deferred],
            "viability": {
                "status": self._heuristic_viability_status(actionable),
                "summary": self._heuristic_viability_summary(actionable, confirmed),
                "constraints": constraints
                or ["No hard blockers identified from the current spec."],
            },
            "pathways": pathways,
            "recommended_pathway_id": recommended,
        }

    def _heuristic_viability_status(self, actionable: list[dict[str, Any]]) -> str:
        if not actionable:
            return "red"
        confirmed = sum(1 for i in actionable if i["status"] == SpecStatus.CONFIRMED.value)
        if confirmed >= 1:
            return "green"
        if len(actionable) >= 2:
            return "amber"
        return "amber"

    def _heuristic_viability_summary(
        self, actionable: list[dict[str, Any]], confirmed: list[dict[str, Any]]
    ) -> str:
        if not actionable:
            return "Not viable yet — no actionable requirements in the spec."
        if confirmed:
            return f"Viable to build — {len(confirmed)} confirmed requirement(s) define a clear path."
        return "Buildable with caution — requirements are still mostly tentative."

    def _default_goal(self, actionable: list[dict[str, Any]]) -> str:
        if not actionable:
            return "Clarify product requirements from the call."
        top = actionable[0]["requirement"]
        if len(actionable) == 1:
            return top
        return f"{top} (plus {len(actionable) - 1} related requirement(s))"

    def _default_pathways(self, actionable: list[dict[str, Any]]) -> list[dict[str, Any]]:
        confirmed = [i for i in actionable if i["status"] == SpecStatus.CONFIRMED.value]
        mvp_items = confirmed or actionable[: max(1, min(3, len(actionable)))]
        return [
            {
                "id": "exact",
                "title": "Exact ask",
                "summary": f"Implement all {len(actionable)} actionable requirement(s) as discussed.",
                "effort": "high" if len(actionable) > 4 else "medium",
                "tradeoffs": "Highest fidelity to the call; may include tentative scope.",
                "approach": "Scaffold app modules for each requirement; wire dummy data where sources are unknown.",
            },
            {
                "id": "mvp",
                "title": "Lean MVP",
                "summary": f"Ship {len(mvp_items)} core item(s) first; defer the rest.",
                "effort": "low",
                "tradeoffs": "Faster path; some call asks wait for a follow-up build.",
                "approach": "Prioritize confirmed items (or top tentative), keep UI/desktop-only, fixture data.",
            },
            {
                "id": "efficient",
                "title": "Efficient alternative",
                "summary": "Reuse patterns and simplify integrations for a faster, testable slice.",
                "effort": "medium",
                "tradeoffs": "May substitute mocks/fixtures for live integrations initially.",
                "approach": (
                    "Generate a thin vertical slice with dummy-data pipeline smoke tests; "
                    "leave hooks for prior-repo integrations (e.g. SharePoint) as follow-ups."
                ),
            },
        ]

    def _normalize_pathway(self, raw: dict[str, Any], idx: int) -> dict[str, Any]:
        return {
            "id": str(raw.get("id") or f"path_{idx + 1}"),
            "title": str(raw.get("title") or f"Pathway {idx + 1}"),
            "summary": str(raw.get("summary") or ""),
            "effort": str(raw.get("effort") or "medium"),
            "tradeoffs": str(raw.get("tradeoffs") or ""),
            "approach": str(raw.get("approach") or ""),
        }


brief_service = BriefService()
