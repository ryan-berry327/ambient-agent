"""Cursor Cloud Agents REST API client (no local bridge required)."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

CURSOR_API_BASE = "https://api.cursor.com/v1"
TERMINAL_STATUSES = {"FINISHED", "FAILED", "CANCELLED", "ERROR"}


class CursorApiError(Exception):
    pass


class CursorApiClient:
    def __init__(self, api_key: str | None = None, timeout: float = 180.0) -> None:
        self.api_key = api_key or settings.cursor_api_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def prompt(self, text: str, model: str | None = None) -> str:
        """Create a no-repo cloud agent, wait for the run, return final text."""
        model_id = model or settings.cursor_model
        payload = {
            "prompt": {"text": text},
            "model": {"id": model_id},
        }
        with httpx.Client(timeout=self.timeout) as client:
            create = client.post(
                f"{CURSOR_API_BASE}/agents",
                headers=self._headers(),
                json=payload,
            )
            if create.status_code >= 400:
                raise CursorApiError(f"Create agent failed: {create.status_code} {create.text[:300]}")
            data = create.json()
            agent = data.get("agent") or {}
            run = data.get("run") or {}
            agent_id = agent.get("id") or run.get("agentId")
            run_id = run.get("id") or agent.get("latestRunId")
            if not agent_id or not run_id:
                raise CursorApiError(f"Missing agent/run ids in response: {data}")

            deadline = time.time() + self.timeout
            while time.time() < deadline:
                run_resp = client.get(
                    f"{CURSOR_API_BASE}/agents/{agent_id}/runs/{run_id}",
                    headers=self._headers(),
                )
                if run_resp.status_code >= 400:
                    raise CursorApiError(f"Get run failed: {run_resp.status_code} {run_resp.text[:300]}")
                run_data: dict[str, Any] = run_resp.json()
                status = str(run_data.get("status", "")).upper()
                if status in TERMINAL_STATUSES:
                    if status != "FINISHED":
                        raise CursorApiError(f"Run ended with status {status}")
                    result = run_data.get("result") or ""
                    logger.info("Cursor run finished agent=%s run=%s", agent_id, run_id)
                    return str(result)
                time.sleep(2)

            raise CursorApiError("Cursor run timed out")


cursor_client = CursorApiClient()
