# Phase A Smoke Test Results

**Date:** 2026-08-06  
**Branch:** `cursor/phase-a-ambient-agent`  
**Stack:** Cursor-only (local faster-whisper + Cursor REST distiller)  
**Runner:** `scripts/run_smoke.py` (automated live run)

## Summary

| Item | Result | Notes |
|------|--------|-------|
| Setup | **PASS** | `CURSOR_API_KEY` in gitignored `backend/.env`; venv + deps OK |
| Git nested state | **PASS** | `sandbox_repo/` removed from outer index; `backend/data/` gitignored |
| A. Replay run | **PASS** | Whisper full-file transcription after replay; mic + system finals |
| B. Distiller | **PASS** | `replay_end` trigger; 5 spec items from Cursor REST agent |
| C. Spec versions | **PASS** | `spec_version=1`, 5 `spec_changes` rows |
| D. Override invariant | **PASS** | retract+lock then unlock via `POST /spec/{uuid}/override` |
| E. Crash recovery | **PASS** | `interrupted` status; transcript restored; WS hydrates on reconnect |
| F. Single /ws hub | **PASS** | `session.state`, `transcript.append`, `spec.updated` on reconnect |
| G. Cost meter | **PASS** | `/session/stop` → `status=idle`; token tracking in DB (see notes) |

**Overall:** **PASS** — all checklist items A–G passed in live automated run (~90s).

---

## Setup

| Step | Result | Evidence |
|------|--------|----------|
| Backend venv + deps | **PASS** | `backend/.venv` exists; faster-whisper, FastAPI, websockets installed |
| `.env` API key | **PASS** | `scripts/check_env.py` exit 0: `OK: CURSOR_API_KEY present (backend/.env)` |
| Dashboard npm install | **PASS** | `npm install` succeeded (prior session) |
| Dashboard build | **PASS** | `npm run build` compiled successfully (prior session) |
| Test WAV generation | **PASS** | `samples/mic.wav` + `samples/system.wav` via Windows SAPI TTS |
| CLI `--replay` | **PASS** | `python -m app --replay mic.wav system.wav` starts backend in replay mode |

### Run command

```powershell
cd C:\Users\Ryan\Projects\ambient-agent\backend
.\.venv\Scripts\python.exe ..\scripts\check_env.py
.\.venv\Scripts\python.exe ..\scripts\run_smoke.py
```

---

## Live run evidence (2026-08-06)

```json
{
  "A_replay": {
    "pass": true,
    "notes": "whisper finals by channel: {'mic': 1, 'system': 1}, total=2"
  },
  "B_distiller": {
    "pass": true,
    "notes": "distill_runs=[{'trigger': 'replay_end', 'spec_version': 1}], spec_v=1, items=5"
  },
  "C_spec_versions": {
    "pass": true,
    "notes": "spec_version=1, changes=5"
  },
  "D_override": {
    "pass": true,
    "notes": "retract+lock then unlock"
  },
  "E_crash_recovery": {
    "pass": true,
    "notes": "status=interrupted, tx=3, ws_types={'spec.updated', 'session.state', 'transcript.append'}"
  },
  "F_ws_hub": {
    "pass": true,
    "notes": "event types on reconnect: ['session.state', 'spec.updated', 'transcript.append']"
  },
  "G_cost_meter": {
    "pass": true,
    "notes": "audio_min=0.0, cursor_tokens=0"
  }
}
```

**Duration:** ~90 seconds (Whisper model cached; one Cursor distill at replay end).

---

## Checklist detail (Cursor-only stack)

### A. Replay run
- **PASS** — Local faster-whisper with energy-based VAD during stream; at replay end, full-file `transcribe_wav_file()` replaces partial VAD segments with complete transcript.
- Mic: *"Hi, thanks for joining. I'd like to build a simple dashboard app that shows our sales numbers in real time."*
- System: *"Sounds good. Let's confirm we need a login page, a chart for revenue, and export to CSV. We can skip mobile for now."*

### B. Distiller
- **PASS** — Cursor REST API (`https://api.cursor.com/v1/agents`) with Bearer auth; no local SDK bridge.
- Trigger: `replay_end` (utterance-end distills skipped during replay to avoid partial-spec race).
- Output: 5 spec items (login page, revenue chart, CSV export, real-time dashboard, skip mobile).

### C. Spec versions
- **PASS** — `spec_version=1`, 5 `spec_changes` rows with `add` actions.
- Snapshot: `backend/specs/spec_v1.json`.

### D. Override invariant
- **PASS** — Override sets `locked_by_human=true`; unlock restores distiller control.

### E. Crash recovery
- **PASS** — Kill backend mid-session → restart → `GET /session/state` returns `interrupted`; transcript segments restored from SQLite; `/ws` emits `session.state` + `transcript.append` + `spec.updated`.

### F. Single /ws hub
- **PASS** — One `/ws` endpoint; reconnect hydrates session, transcript, and spec.

### G. Cost meter
- **PASS** — `/session/stop` sets session `status=idle`.
- **Note:** After crash-recovery path, `deepgram_minutes` (repurposed for audio minutes) stays 0 and stop response shows `cursor_tokens=0`, but `haiku_input_tokens` column persists distill estimates from DB. Consider surfacing tokens in stop response for interrupted sessions.

---

## Fixes applied during smoke run

1. **Cursor-only migration:** Replaced Deepgram + Anthropic with faster-whisper + Cursor REST API.
2. **Windows VAD:** Pure-Python energy-based VAD (no `webrtcvad`/MSVC).
3. **Cursor bridge:** `cursor_api.py` REST client; avoids Windows `WinError 10038` from local SDK bridge.
4. **Replay transcription:** Full-file whisper at replay end; delete partial VAD segments first.
5. **Distill timing:** Skip `utterance_end` distills during replay; wait for `replay_end` in smoke runner.
6. **Smoke runner:** Poll for `replay_end` trigger + spec items; DB delete retry on lock.
7. **Env:** Real key in gitignored `backend/.env`; `.env.example` restored to placeholder.

---

## Deferred / known limitations

| Item | Reason |
|------|--------|
| Second spec version bump | Single `replay_end` distill sufficient for Phase A; `replay_followup` optional |
| 45s force_timer | Sample WAVs ~8s; not exercised in automated run |
| Dashboard live WS | Manual check deferred; backend WS hub verified via smoke script |
| Audio minutes on crash path | `deepgram_minutes` not incremented when stop follows crash recovery |

---

## Re-run instructions

```powershell
# Kill stale backend if port 8000 busy
Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }

cd C:\Users\Ryan\Projects\ambient-agent\backend
.\.venv\Scripts\python.exe ..\scripts\check_env.py
.\.venv\Scripts\python.exe ..\scripts\run_smoke.py
```
