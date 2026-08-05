# Ambient Call Agent — Phase A

An ambient call agent that listens to conversations (mic + system audio), transcribes locally with Whisper, distills requirements into a living spec via the **Cursor SDK**, and displays everything on a live dashboard.

**Billing:** Only Cursor API usage is paid. Transcription runs locally (free).

## Architecture

```
ambient-agent/
├── backend/          FastAPI + SQLite + Whisper + Cursor SDK
├── dashboard/        Next.js single-page UI
├── sandbox_repo/     Vite + React skeleton (Phase B builder target)
├── samples/          Test WAV files for replay mode
└── scripts/          Utilities (test WAV generator, smoke test)
```

## Prerequisites

- **Python 3.12+**
- **Node.js 20+**
- **Windows** with WASAPI audio (for live capture)
- **Cursor API key** — [cursor.com/settings](https://cursor.com/settings) or your Cursor account API settings

## Environment setup

```powershell
cd backend
copy .env.example .env
# Set CURSOR_API_KEY=crsr_...

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

cd ..\dashboard
npm install
```

### `.env` variables

| Variable | Description | Default |
|----------|-------------|---------|
| `CURSOR_API_KEY` | Cursor API key | **required** |
| `CURSOR_MODEL` | Model for distiller | `composer-2.5` |
| `REPLAY_MODE` | Use WAV replay instead of live audio | `true` |
| `WHISPER_MODEL` | Local STT model (`tiny`, `base`, `small`) | `base` |
| `WHISPER_DEVICE` | `cpu` or `cuda` | `cpu` |

First run downloads the Whisper model (~150 MB for `base`).

## Generate test WAV files

```powershell
python scripts/generate_test_wavs.py
```

## Running

**Backend:**

```powershell
cd backend
.venv\Scripts\activate
python -m app --replay ../samples/mic.wav ../samples/system.wav
```

**Dashboard:**

```powershell
cd dashboard
npm run dev
```

Open [http://localhost:3000](http://localhost:3000) → **Start session**.

## How transcription works

- **Mic** and **system** audio captured as two independent streams at native sample rates
- Each stream: **energy-based VAD** detects utterance boundaries → **faster-whisper** transcribes locally
- Mic labelled `me`, system labelled `remote` (no cloud diarization needed)
- Interim `…` shown while speech is detected; finals persisted to SQLite

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/session/start` | Start capture + transcription |
| POST | `/session/stop` | Stop session |
| GET | `/devices` | List WASAPI input + loopback devices |
| GET | `/transcript` | Final transcript segments |
| GET | `/spec` | Current spec items |
| GET | `/spec/changes` | Spec change log |
| POST | `/spec/{uuid}/override` | Confirm/retract/unlock |
| WS | `/ws` | Typed events |

## Cost estimate

| Component | Cost |
|-----------|------|
| Whisper transcription | **$0** (local CPU/GPU) |
| Cursor distiller | ~$0.01–0.05 per distill run (model-dependent) |
| **Typical 1hr call** | **~$0.50–2.00** (mostly Cursor distill + future builder) |

## Phase B (not yet implemented)

Builder agent targeting `sandbox_repo/` via Cursor SDK, auto-build, preview, reports.
