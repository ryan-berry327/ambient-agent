#!/usr/bin/env python3
"""Automated Phase A smoke test runner. Writes evidence to stdout."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
DB = BACKEND / "data" / "ambient.db"
API = "http://127.0.0.1:8000"
WS = "ws://127.0.0.1:8000/ws"
PYTHON = BACKEND / ".venv" / "Scripts" / "python.exe"
MIC = ROOT / "samples" / "mic.wav"
SYSTEM = ROOT / "samples" / "system.wav"


def http(method: str, path: str, body: dict | None = None) -> dict:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{API}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def db_query(sql: str, params: tuple = ()) -> list:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


async def collect_ws(duration: float) -> list[dict]:
    import websockets

    events: list[dict] = []
    async with websockets.connect(WS) as ws:
        end = time.time() + duration
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                events.append(json.loads(raw))
            except asyncio.TimeoutError:
                continue
    return events


def wait_api(timeout: float = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            http("GET", "/session/state")
            return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> int:
    results: dict[str, dict] = {}

    if not PYTHON.exists():
        print("FAIL setup: backend venv missing")
        return 1

    check = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_env.py")], capture_output=True, text=True)
    if check.returncode != 0:
        print(check.stdout + check.stderr)
        return 1

    # Clean DB for deterministic run
    if DB.exists():
        for _ in range(5):
            try:
                DB.unlink()
                break
            except PermissionError:
                time.sleep(1)

    proc = subprocess.Popen(
        [
            str(PYTHON),
            "-m",
            "app",
            "--replay",
            str(MIC),
            str(SYSTEM),
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        cwd=str(BACKEND),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        if not wait_api():
            results["setup"] = {"pass": False, "notes": "Backend did not start"}
            print(json.dumps(results, indent=2))
            return 1

        # A: start session + replay
        state = http("POST", "/session/start", {})
        session_id = state["session_id"]

        # Poll for transcript (Whisper + model load can take 60-120s first run)
        channels: dict[str, int] = {}
        for _ in range(60):
            segs = db_query(
                "SELECT channel, COUNT(*) c FROM transcript_segments WHERE session_id=? AND is_final=1 GROUP BY channel",
                (session_id,),
            )
            channels = {r["channel"]: r["c"] for r in segs}
            if channels.get("mic", 0) > 0 and channels.get("system", 0) > 0:
                break
            time.sleep(3)
        total = sum(channels.values())
        results["A_replay"] = {
            "pass": total > 0 and "mic" in channels and "system" in channels,
            "notes": f"whisper finals by channel: {channels}, total={total}",
        }

        # Poll for replay-end distill (Cursor cloud agent; ~60-90s per run)
        spec_v = 0
        runs = []
        item_count = 0
        for _ in range(120):
            versions = db_query("SELECT spec_version FROM sessions WHERE id=?", (session_id,))
            spec_v = versions[0]["spec_version"] if versions else 0
            runs = db_query(
                "SELECT trigger, spec_version FROM distill_runs WHERE session_id=? ORDER BY id",
                (session_id,),
            )
            item_count = db_query(
                "SELECT COUNT(*) c FROM spec_items WHERE session_id=?",
                (session_id,),
            )[0]["c"]
            triggers = [r["trigger"] for r in runs]
            replay_done = any("replay_end" in t or "replay_followup" in t for t in triggers)
            if replay_done and item_count >= 1:
                break
            if spec_v >= 2 and item_count >= 1:
                break
            time.sleep(3)
        results["B_distiller"] = {
            "pass": len(runs) >= 1 and spec_v >= 1 and item_count >= 1,
            "notes": f"distill_runs={[dict(r) for r in runs]}, spec_v={spec_v}, items={item_count}",
        }

        changes = db_query(
            "SELECT spec_version, action, reason, item_uuid FROM spec_changes WHERE session_id=? ORDER BY spec_version, id",
            (session_id,),
        )
        results["C_spec_versions"] = {
            "pass": spec_v >= 2 or (spec_v >= 1 and len(changes) >= 1),
            "notes": f"spec_version={spec_v}, changes={len(changes)}",
        }

        # D: override
        items = db_query("SELECT uuid, status, locked_by_human FROM spec_items WHERE session_id=? LIMIT 1", (session_id,))
        override_ok = False
        if items:
            uid = items[0]["uuid"]
            http("POST", f"/spec/{uid}/override", {"status": "retracted"})
            locked = db_query("SELECT locked_by_human, status FROM spec_items WHERE uuid=?", (uid,))
            override_ok = locked and locked[0]["locked_by_human"] == 1 and locked[0]["status"] == "retracted"
            http("POST", f"/spec/{uid}/override", {"status": "confirmed", "unlock": True})
        results["D_override"] = {"pass": override_ok, "notes": "retract+lock then unlock" if items else "no items to override"}

        # E: crash recovery
        ws_types: set[str] = set()
        proc.kill()
        proc.wait(timeout=5)
        time.sleep(1)
        proc2 = subprocess.Popen(
            [str(PYTHON), "-m", "app", "--host", "127.0.0.1", "--port", "8000"],
            cwd=str(BACKEND),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if not wait_api():
            results["E_crash_recovery"] = {"pass": False, "notes": "Backend restart failed"}
        else:
            recovered = http("GET", "/session/state")
            tx = http("GET", "/transcript")
            sp = http("GET", "/spec")
            ws_events = asyncio.run(collect_ws(3))
            ws_types = {e["type"] for e in ws_events}
            results["E_crash_recovery"] = {
                "pass": recovered.get("status") == "interrupted"
                and len(tx.get("segments", [])) > 0
                and recovered.get("spec_version", 0) >= spec_v
                and "session.state" in ws_types
                and any(e["type"] == "transcript.append" for e in ws_events),
                "notes": f"status={recovered.get('status')}, tx={len(tx.get('segments',[]))}, ws_types={ws_types}",
            }
        proc = proc2

        # F: ws hub (from collected events + reconnect)
        results["F_ws_hub"] = {
            "pass": "session.state" in ws_types and any(t in ws_types for t in ("transcript.append", "spec.updated")),
            "notes": f"event types on reconnect: {sorted(ws_types)}",
        }

        # G: cost meter on stop
        before = http("GET", "/session/state")
        stopped = http("POST", "/session/stop", {})
        row = db_query(
            "SELECT deepgram_minutes, haiku_input_tokens, haiku_output_tokens, status FROM sessions WHERE id=?",
            (session_id,),
        )
        results["G_cost_meter"] = {
            "pass": row
            and row[0]["status"] == "idle"
            and (
                row[0]["deepgram_minutes"] > 0
                or row[0]["haiku_input_tokens"] > 0
                or stopped.get("haiku_input_tokens", 0) > 0
            ),
            "notes": f"audio_min={row[0]['deepgram_minutes'] if row else 0}, cursor_tokens={stopped.get('haiku_input_tokens')}",
        }

        print(json.dumps(results, indent=2))
        return 0 if all(r.get("pass") for r in results.values()) else 1
    finally:
        if proc.poll() is None:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
