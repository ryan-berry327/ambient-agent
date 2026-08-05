# Ambient Call Agent — Phase A

An ambient call agent that listens to conversations (mic + system audio), transcribes via Deepgram, distills requirements into a living spec via Claude Haiku, and displays everything on a live dashboard.

## Architecture

```
ambient-agent/
├── backend/          FastAPI + SQLite + Deepgram + Anthropic
├── dashboard/        Next.js single-page UI
├── sandbox_repo/     Vite + React skeleton (Phase B builder target)
├── samples/          Test WAV files for replay mode
└── scripts/          Utilities (test WAV generator)
```

## Prerequisites

- **Python 3.12**
- **Node.js 20+**
- **Windows** with WASAPI audio (for live capture)
- API keys:
  - [Deepgram](https://console.deepgram.com/) — live streaming transcription
  - [Anthropic](https://console.anthropic.com/) — spec distillation (Haiku)

## Environment setup

```powershell
# Backend
cd backend
copy .env.example .env
# Edit .env and set DEEPGRAM_API_KEY and ANTHROPIC_API_KEY
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# Dashboard
cd ..\dashboard
npm install

# Sandbox (pre-seeded for Phase B)
cd ..\sandbox_repo
npm install
git init
git add .
git commit -m "Initial Vite + React skeleton"
```

### `.env` variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DEEPGRAM_API_KEY` | Deepgram API key | required |
| `ANTHROPIC_API_KEY` | Anthropic API key | required |
| `REPLAY_MODE` | Use WAV replay instead of live audio | `true` |
| `REPLAY_MIC_WAV` | Path to mic channel WAV | `../samples/mic.wav` |
| `REPLAY_SYSTEM_WAV` | Path to system channel WAV | `../samples/system.wav` |
| `DISTILL_MIN_INTERVAL_SEC` | Min seconds between distill runs | `10` |
| `DISTILL_FORCE_INTERVAL_SEC` | Force distill if pending text older than | `45` |

## Generate test WAV files

Replay mode is **on by default** so you can develop without a live call:

```powershell
python scripts/generate_test_wavs.py
```

This creates `samples/mic.wav` and `samples/system.wav`. For richer tests, replace them with real recordings or TTS output (16 kHz mono PCM recommended, but native rates work).

## Running

**Terminal 1 — Backend:**

```powershell
cd backend
.venv\Scripts\activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Terminal 2 — Dashboard:**

```powershell
cd dashboard
npm run dev
```

Open [http://localhost:3000](http://localhost:3000), click **Start session**, and watch the transcript + spec update live.

## Device selection (live mode)

Set `REPLAY_MODE=false` in `.env` and restart the backend.

1. The dashboard top bar shows **Mic** and **System** dropdowns populated from `GET /devices`.
2. **Mic** = your default input device (you are always labelled `me`).
3. **System** = WASAPI loopback device (captures call audio from speakers/headset). Look for "(default loopback)" in the name.
4. Both streams capture at their **native sample rates** — no muxing. Each gets its own Deepgram websocket connection.

> **Note:** Some apps use exclusive audio mode which can block loopback capture. If system audio is silent, disable exclusive mode in Windows Sound settings or use a virtual audio cable.

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/session/start` | Start capture + transcription |
| POST | `/session/stop` | Stop session |
| GET | `/devices` | List WASAPI input + loopback devices |
| GET | `/transcript` | Final transcript segments |
| GET | `/spec` | Current spec items |
| GET | `/spec/changes` | Spec change log (journey tab) |
| POST | `/spec/{uuid}/override` | Confirm/retract/unlock a spec item |
| WS | `/ws` | Typed events: `transcript.append`, `spec.updated`, `distill.*`, `session.state` |

## Definition of done (Phase A)

1. Replay of `samples/mic.wav` + `samples/system.wav` produces a merged labelled transcript.
2. ≥2 spec versions appear with visible deltas in the **Spec journey** tab.
3. A manual **Confirm/Retract** override survives the next distill cycle untouched (`locked_by_human=true`).

## Rough cost estimate

Per hour of active session (approximate):

| Service | Usage | Cost |
|---------|-------|------|
| Deepgram Nova-2 streaming | 2 channels × 60 min | ~$0.52 |
| Claude Haiku 4.5 distiller | ~80 runs/hr × ~2K tokens | ~$0.20 |
| **Total** | | **~$0.70–1.00/hr** |

Replay mode with short sample files costs pennies per test run.

## Phase B (not yet implemented)

- Builder agent (`POST /build`) targeting `sandbox_repo/`
- Auto-build triggers, git diff view, preview dev server
- Post-call report generator

The **Build now** button is present but disabled.
