# Phase A Smoke Test Results

**Date:** 2026-08-05  
**Branch:** `cursor/phase-a-ambient-agent`  
**Runner:** `scripts/run_smoke.py` (automated) + manual offline checks

## Summary

| Item | Result | Notes |
|------|--------|-------|
| Setup | **HALT** | API keys missing — live replay blocked |
| Git nested state | **PASS** | `sandbox_repo/` removed from outer index; `backend/data/` gitignored |
| A. Replay run | **BLOCKED** | Requires `DEEPGRAM_API_KEY` |
| B. Distiller | **BLOCKED** | Requires `ANTHROPIC_API_KEY` + Deepgram transcript |
| C. Spec versions | **BLOCKED** | Depends on B |
| D. Override invariant | **BLOCKED** | Depends on C |
| E. Crash recovery | **BLOCKED** | Depends on A (needs live session in SQLite) |
| F. Single /ws hub | **BLOCKED** | Depends on A |
| G. Cost meter | **BLOCKED** | Depends on A + B |

**Overall:** Cannot complete live checklist without API keys. Offline infrastructure checks pass.

---

## Setup

| Step | Result | Evidence |
|------|--------|----------|
| Backend venv + deps | **PASS** | `backend/.venv` exists; `pip install -r requirements.txt` succeeded |
| `.env` API keys | **HALT** | `scripts/check_env.py` exit 1: `DEEPGRAM_API_KEY`, `ANTHROPIC_API_KEY` missing from `backend/.env` and environment |
| Dashboard npm install | **PASS** | `npm install` succeeded |
| Dashboard build | **PASS** | `npm run build` compiled successfully (Next.js 15) |
| Test WAV generation | **PASS** | `scripts/generate_test_wavs.py` produced `samples/mic.wav` + `samples/system.wav` via Windows SAPI TTS |
| CLI `--replay` | **PASS** | `python -m app --help` shows `--replay MIC_WAV SYSTEM_WAV` |

### Action required to unblock live tests

```powershell
cd backend
copy .env.example .env
# Set DEEPGRAM_API_KEY and ANTHROPIC_API_KEY
python ..\scripts\run_smoke.py
```

---

## Git nested state

| Check | Result | Evidence |
|-------|--------|----------|
| `git ls-files sandbox_repo/` | **PASS** | Empty output — no embedded gitlink or partial track |
| `sandbox_repo/` in `.gitignore` | **PASS** | Added; nested repo stays local for Phase B builder |
| `backend/data/` gitignored | **PASS** | `git check-ignore -v backend/data/` → `.gitignore:13:backend/data/` |

---

## Checklist (live — blocked)

### A. Replay run
**BLOCKED** — no Deepgram key. Expected when unblocked:
- Start: `python -m app --replay ../samples/mic.wav ../samples/system.wav`
- Two Deepgram WS connections; transcript segments tagged `mic|system`
- Finals persisted to `transcript_segments`; interims over WS with `is_final=false`

### B. Distiller
**BLOCKED** — no Anthropic key. Offline parser hardening verified (see Fixes). Expected when unblocked:
- Triggers: `utterance_end:*`, `replay_end`, `force_timer` (≥45s with pending text)
- Logging added: `Distill scheduled`, `Distill skipped (spacing)`, `Distill force_timer`
- JSON parse with fence strip + object extraction + one retry

### C. Spec versions
**BLOCKED** — depends on B. Expected: ≥2 `spec_version` bumps, `spec_changes` rows with action+reason.

### D. Override invariant
**BLOCKED** — depends on C. Code path verified: `POST /spec/{uuid}/override` sets `locked_by_human=true`; distiller re-applies locked items from DB.

### E. Crash recovery
**BLOCKED** — depends on A. Fix applied: startup calls `recover_interrupted_sessions()`; `GET /session/state` returns `interrupted`; `/ws` hydrates transcript+spec from SQLite on connect.

### F. Single /ws hub
**BLOCKED** — depends on A. Architecture confirmed: one `/ws` endpoint emits `transcript.append`, `spec.updated`, `distill.started/finished`, `session.state`.

### G. Cost meter
**BLOCKED** — depends on A+B. Code path: `sessions.deepgram_minutes` updated on `/session/stop`; Haiku tokens accumulated per distill run.

---

## Offline verification (passed)

| Test | Result |
|------|--------|
| Distiller JSON parser (fences, embedded object) | **PASS** — `backend/tests/test_parser.py` |
| Backend import + DB init | **PASS** — `from app.main import app` |
| Device enumeration API fix | **PASS** (from prior session) — `GET /devices` uses `get_device_count()` |

---

## Fixes applied during smoke run

1. **Git:** Added `sandbox_repo/` to outer `.gitignore`; `git rm -r --cached sandbox_repo` to drop embedded/partial track.
2. **CLI:** Added `backend/app/__main__.py` with `--replay mic.wav system.wav` flag.
3. **Distiller:** Hardened JSON parser (fence strip, regex object extract); retry once on parse failure; trigger logging.
4. **Crash recovery:** `recover_interrupted_sessions()` on startup; `/session/stop` handles interrupted DB sessions without live audio.
5. **Env check:** `scripts/check_env.py` accepts keys from `.env` or environment (never prints values).
6. **Smoke runner:** `scripts/run_smoke.py` automated checklist (run after keys added).

---

## Deferred items

| Item | Reason |
|------|--------|
| A–G live pass | No `DEEPGRAM_API_KEY` / `ANTHROPIC_API_KEY` on test machine |
| 45s force_timer observation | Sample WAVs ~8s; force_timer requires ≥45s elapsed with pending text — use longer WAVs or lower `DISTILL_FORCE_INTERVAL_SEC` for test |
| Dashboard live WS reconnect | Requires backend running with active session |

---

## Re-run instructions

```powershell
# 1. Keys
cd C:\Users\Ryan\Projects\ambient-agent\backend
copy .env.example .env   # edit keys

# 2. Automated smoke
cd ..
backend\.venv\Scripts\python.exe scripts\run_smoke.py

# 3. Manual dashboard check
cd dashboard
npm run dev
# Open http://localhost:3000, Start session, verify UI
```

After keys are set, update this file with live pass/fail evidence and re-commit.
