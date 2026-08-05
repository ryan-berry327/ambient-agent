"""Anthropic distiller: transcript -> spec with structured deltas."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import anthropic

from app.config import settings
from app.database import (
    DistillRunModel,
    SessionLocal,
    SessionModel,
    SpecChangeModel,
    SpecItemModel,
    SpecStatus,
    utcnow,
)
from app.schemas import DistillOutput
from app.services.ws_hub import ws_hub

logger = logging.getLogger(__name__)

DISTILL_PROMPT = """You are a requirements distiller for a live client call.

Given the recent transcript and the current spec, produce an UPDATED spec as strict JSON.

Output format (JSON only, no markdown fences):
{
  "changes": [
    {"id": "<uuid>", "action": "add|update|retract", "reason": "<why>"}
  ],
  "spec": [
    {
      "id": "<uuid>",
      "requirement": "<clear requirement statement>",
      "status": "confirmed|tentative|retracted",
      "evidence_quote": "<direct quote from transcript>",
      "category": "ui|api|data|auth|general",
      "acceptance_hint": "<optional one-liner or null>",
      "supersedes": "<uuid of replaced item or null>"
    }
  ]
}

Rules:
- Reuse existing UUIDs when a requirement is reworded; mint new UUIDs only for genuinely new items.
- When replacing an item, set supersedes on the new item to the old UUID and retract the old one.
- Items with locked_by_human=true are IMMUTABLE: echo them back unchanged, never alter their status or text.
- confirmed = explicitly agreed; tentative = mentioned but not agreed; retracted = explicitly rejected or superseded.
- Only include items with evidence in the transcript. Be conservative with confirmed.
- Return ONLY valid JSON."""


class DistillerService:
    def __init__(self) -> None:
        self._running = False
        self._last_distill_at: Optional[datetime] = None
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    @property
    def is_running(self) -> bool:
        return self._running

    async def maybe_distill(
        self,
        session_id: str,
        trigger: str = "utterance_end",
        force: bool = False,
    ) -> bool:
        if self._running:
            return False

        db = SessionLocal()
        try:
            session = db.get(SessionModel, session_id)
            if not session or session.status != "running":
                return False

            from app.database import TranscriptSegmentModel

            new_finals = (
                db.query(TranscriptSegmentModel)
                .filter(
                    TranscriptSegmentModel.session_id == session_id,
                    TranscriptSegmentModel.is_final == True,  # noqa: E712
                    TranscriptSegmentModel.distilled == False,  # noqa: E712
                )
                .count()
            )
            if new_finals == 0 and not force:
                return False

            now = utcnow()
            if not force and self._last_distill_at:
                elapsed = (now - self._last_distill_at).total_seconds()
                if elapsed < settings.distill_min_interval_sec:
                    logger.info(
                        "Distill skipped (spacing) session=%s trigger=%s elapsed=%.1fs min=%s",
                        session_id,
                        trigger,
                        elapsed,
                        settings.distill_min_interval_sec,
                    )
                    return False

            logger.info("Distill scheduled session=%s trigger=%s force=%s pending=%s", session_id, trigger, force, new_finals)
            await self._run_distill(session_id, trigger)
            return True
        finally:
            db.close()

    async def force_if_stale(self, session_id: str) -> None:
        db = SessionLocal()
        try:
            from app.database import TranscriptSegmentModel

            pending = (
                db.query(TranscriptSegmentModel)
                .filter(
                    TranscriptSegmentModel.session_id == session_id,
                    TranscriptSegmentModel.is_final == True,  # noqa: E712
                    TranscriptSegmentModel.distilled == False,  # noqa: E712
                )
                .count()
            )
            if pending == 0:
                return
            now = utcnow()
            if self._last_distill_at:
                elapsed = (now - self._last_distill_at).total_seconds()
                if elapsed >= settings.distill_force_interval_sec:
                    logger.info(
                        "Distill force_timer session=%s elapsed=%.1fs pending=%s",
                        session_id,
                        elapsed,
                        pending,
                    )
                    await self.maybe_distill(session_id, trigger="force_timer", force=True)
        finally:
            db.close()

    async def _run_distill(self, session_id: str, trigger: str) -> None:
        self._running = True
        started = utcnow()
        await ws_hub.broadcast("distill.started", {"session_id": session_id, "trigger": trigger})

        db = SessionLocal()
        try:
            session = db.get(SessionModel, session_id)
            if not session:
                return

            from app.database import TranscriptSegmentModel

            cutoff = utcnow() - timedelta(minutes=3)
            segments = (
                db.query(TranscriptSegmentModel)
                .filter(
                    TranscriptSegmentModel.session_id == session_id,
                    TranscriptSegmentModel.is_final == True,  # noqa: E712
                    TranscriptSegmentModel.ts >= cutoff,
                )
                .order_by(TranscriptSegmentModel.ts.asc())
                .all()
            )
            transcript_text = "\n".join(
                f"[{s.ts.isoformat()}] ({s.channel}/{s.speaker}): {s.text}" for s in segments
            )

            current_items = (
                db.query(SpecItemModel)
                .filter(SpecItemModel.session_id == session_id)
                .all()
            )
            current_spec = [
                {
                    "id": item.uuid,
                    "requirement": item.requirement,
                    "status": item.status,
                    "evidence_quote": item.evidence_quote,
                    "category": item.category,
                    "acceptance_hint": item.acceptance_hint,
                    "supersedes": item.supersedes,
                    "locked_by_human": item.locked_by_human,
                }
                for item in current_items
            ]

            user_content = (
                f"## Recent transcript (last 3 min)\n{transcript_text or '(no transcript yet)'}\n\n"
                f"## Current spec\n{json.dumps(current_spec, indent=2)}"
            )

            response = self._client.messages.create(
                model=settings.anthropic_model,
                max_tokens=4096,
                system=DISTILL_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )

            raw_text = ""
            for block in response.content:
                if block.type == "text":
                    raw_text += block.text

            parsed, parse_ok = self._parse_distill_output(raw_text, current_items)
            if not parse_ok:
                logger.warning("Distill JSON parse failed; retrying once session=%s", session_id)
                retry = self._client.messages.create(
                    model=settings.anthropic_model,
                    max_tokens=4096,
                    system=DISTILL_PROMPT,
                    messages=[
                        {"role": "user", "content": user_content},
                        {
                            "role": "user",
                            "content": "Your previous reply was not valid JSON. Return ONLY the JSON object, no markdown fences.",
                        },
                    ],
                )
                retry_text = "".join(b.text for b in retry.content if b.type == "text")
                response.usage.input_tokens += retry.usage.input_tokens
                response.usage.output_tokens += retry.usage.output_tokens
                parsed, parse_ok = self._parse_distill_output(retry_text, current_items)
                if not parse_ok:
                    logger.error("Distill JSON parse failed after retry session=%s", session_id)
                    raise ValueError("Distiller returned invalid JSON after retry")
            new_version = session.spec_version + 1

            # Apply locked items from DB over model output
            locked_map = {i.uuid: i for i in current_items if i.locked_by_human}
            final_spec_items: list[dict[str, Any]] = []
            seen_ids: set[str] = set()

            for item in parsed.spec:
                item_id = item.get("id") or item.get("uuid", "")
                if item_id in locked_map:
                    locked = locked_map[item_id]
                    final_spec_items.append(
                        {
                            "id": locked.uuid,
                            "requirement": locked.requirement,
                            "status": locked.status,
                            "evidence_quote": locked.evidence_quote,
                            "category": locked.category,
                            "acceptance_hint": locked.acceptance_hint,
                            "supersedes": locked.supersedes,
                            "locked_by_human": True,
                        }
                    )
                    seen_ids.add(item_id)
                else:
                    final_spec_items.append(item)
                    seen_ids.add(item_id)

            for uuid_key, locked in locked_map.items():
                if uuid_key not in seen_ids:
                    final_spec_items.append(
                        {
                            "id": locked.uuid,
                            "requirement": locked.requirement,
                            "status": locked.status,
                            "evidence_quote": locked.evidence_quote,
                            "category": locked.category,
                            "acceptance_hint": locked.acceptance_hint,
                            "supersedes": locked.supersedes,
                            "locked_by_human": True,
                        }
                    )

            # Persist changes
            for change in parsed.changes:
                db.add(
                    SpecChangeModel(
                        session_id=session_id,
                        spec_version=new_version,
                        item_uuid=change.id,
                        action=change.action,
                        reason=change.reason,
                        ts=utcnow(),
                    )
                )

            # Replace spec items for session
            db.query(SpecItemModel).filter(SpecItemModel.session_id == session_id).delete()
            for item in final_spec_items:
                db.add(
                    SpecItemModel(
                        uuid=item["id"],
                        session_id=session_id,
                        requirement=item.get("requirement", ""),
                        status=item.get("status", SpecStatus.TENTATIVE.value),
                        evidence_quote=item.get("evidence_quote", ""),
                        category=item.get("category", "general"),
                        acceptance_hint=item.get("acceptance_hint"),
                        supersedes=item.get("supersedes"),
                        locked_by_human=bool(item.get("locked_by_human", False)),
                        spec_version=new_version,
                    )
                )

            session.spec_version = new_version
            session.haiku_input_tokens += response.usage.input_tokens
            session.haiku_output_tokens += response.usage.output_tokens

            db.add(
                DistillRunModel(
                    session_id=session_id,
                    spec_version=new_version,
                    started_at=started,
                    finished_at=utcnow(),
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    trigger=trigger,
                )
            )

            # Mark segments as distilled
            db.query(TranscriptSegmentModel).filter(
                TranscriptSegmentModel.session_id == session_id,
                TranscriptSegmentModel.is_final == True,  # noqa: E712
                TranscriptSegmentModel.distilled == False,  # noqa: E712
            ).update({"distilled": True})

            db.commit()

            # Snapshot to disk
            snapshot = {
                "version": new_version,
                "changes": [c.model_dump() for c in parsed.changes],
                "spec": final_spec_items,
            }
            snapshot_path = settings.specs_dir / f"spec_v{new_version}.json"
            snapshot_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

            self._last_distill_at = utcnow()

            items_out = [
                {
                    "uuid": item["id"],
                    "requirement": item.get("requirement", ""),
                    "status": item.get("status", "tentative"),
                    "evidence_quote": item.get("evidence_quote", ""),
                    "category": item.get("category", "general"),
                    "acceptance_hint": item.get("acceptance_hint"),
                    "built_at_version": None,
                    "supersedes": item.get("supersedes"),
                    "locked_by_human": bool(item.get("locked_by_human", False)),
                    "spec_version": new_version,
                }
                for item in final_spec_items
            ]

            await ws_hub.broadcast(
                "spec.updated",
                {
                    "version": new_version,
                    "items": items_out,
                    "changes": [c.model_dump() for c in parsed.changes],
                },
            )
            await ws_hub.broadcast("distill.finished", {"session_id": session_id, "version": new_version})
            logger.info("Distill complete session=%s version=%s", session_id, new_version)

        except Exception as exc:
            logger.exception("Distill failed: %s", exc)
            await ws_hub.broadcast("distill.finished", {"session_id": session_id, "error": str(exc)})
        finally:
            db.close()
            self._running = False

    def _parse_distill_output(
        self, raw: str, current_items: list[SpecItemModel]
    ) -> tuple[DistillOutput, bool]:
        cleaned = raw.strip()
        # Strip markdown fences anywhere in response
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = re.sub(r"```(?:json)?", "", cleaned).strip()
        # Extract first JSON object if surrounded by prose
        if not cleaned.startswith("{"):
            match = re.search(r"\{[\s\S]*\}", cleaned)
            if match:
                cleaned = match.group(0)
        fallback_spec = [
            {
                "id": item.uuid,
                "requirement": item.requirement,
                "status": item.status,
                "evidence_quote": item.evidence_quote,
                "category": item.category,
                "acceptance_hint": item.acceptance_hint,
                "supersedes": item.supersedes,
                "locked_by_human": item.locked_by_human,
            }
            for item in current_items
        ]
        try:
            data = json.loads(cleaned)
            return DistillOutput.model_validate(data), True
        except Exception as exc:
            logger.warning("Failed to parse distill JSON: %s raw=%r", exc, raw[:200])
            return DistillOutput(changes=[], spec=fallback_spec), False


distiller = DistillerService()
