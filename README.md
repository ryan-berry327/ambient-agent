# Ambient Call Agent — Phase A

An ambient call agent that listens to conversations (mic + system audio), transcribes **locally with Whisper**, distills requirements via the **Cursor API**, and displays everything on a live dashboard.

**Billing today:** Cursor API only. Transcription is free (local CPU/GPU).  
**Upgrade path:** swap `TRANSCRIPTION_BACKEND=deepgram` when you need live streaming STT (not implemented yet).

## Architecture

```
ambient-agent/
├── backend/          FastAPI + SQLite + Whisper + Cursor distiller
├── dashboard/        Next.js single-page UI
├── sandbox_repo/     Vite + React skeleton (Phase B builder target)
├── samples/          Test WAV files for replay mode
└── scripts/          Utilities
```

## Prerequisites

- **Python 3.12+**
- **Node.js 20+**
- **Windows** with WASAPI audio (for live capture)
- **Cursor API key** in `backend/.env`

## Quick start

```powershell
cd backend
copy .env.example .env
# Set CURSOR_API_KEY=crsr_...

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

python -m app --replay ../samples/mic.wav ../samples/system.wav
```

In another terminal:

```powershell
cd dashboard
npm install
npm run dev
```

Open http://localhost:3000 → **Start session**.

First run downloads the Whisper `base` model (~150 MB).

## How it works

| Layer | Implementation |
|-------|----------------|
| **Transcription** | `faster-whisper` + energy VAD per channel (mic → `me`, system → `remote`) |
| **Replay** | WAVs fed in **real time** (100 ms chunks + sleep), same path as live |
| **Distiller** | Cursor REST API; single worker with queue (no overlapping runs) |
| **Triggers** | `utterance_end` (always on, including replay) + 45s force timer |

## `.env`

| Variable | Default | Description |
|----------|---------|-------------|
| `CURSOR_API_KEY` | — | Required |
| `WHISPER_MODEL` | `base` | `tiny`, `base`, `small`, `medium` |
| `WHISPER_DEVICE` | `cpu` | `cuda` if you have a GPU |
| `TRANSCRIPTION_BACKEND` | `whisper` | Future: `deepgram` for live upgrade |
| `REPLAY_MODE` | `true` | Use sample WAVs instead of mic/loopback |

## Upgrading to live streaming later

When you're ready for production calls:

1. Add `DEEPGRAM_API_KEY` to `.env`
2. Implement `DeepgramLiveClient` behind `TRANSCRIPTION_BACKEND=deepgram`
3. Keep Whisper as fallback for offline/dev

The replay and live paths should share the same websocket client interface so the dashboard doesn't change.

## Phase B — Brief → Build

After distillation, the app can turn the spec into a **build brief** with:

1. Actionable vs deferred requirements  
2. Viability (`green` / `amber` / `red`) + constraints  
3. Alternative pathways (exact / MVP / efficient)  
4. **Build now** — scaffolds a project under `sandbox_builds/`, runs a dummy-data smoke test, and optionally pushes to GitHub

| Endpoint | Purpose |
|----------|---------|
| `POST /brief/generate` | Create brief + viability + pathways |
| `POST /brief/select-pathway` | Choose pathway (`{ "pathway_id": "mvp" }`) |
| `POST /build/start` | Scaffold, smoke-test, push |
| `GET /brief` / `GET /build/latest` | Current brief / latest build |

Optional `.env` for push:

```
GITHUB_TOKEN=ghp_...
GITHUB_REPO=owner/repo-name
```

Without GitHub credentials, builds stay local (`push_status=local_only`) after a passing smoke test.
