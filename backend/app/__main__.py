"""CLI entry: python -m app [--replay mic.wav system.wav]"""

from __future__ import annotations

import argparse

import uvicorn

from app.config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Ambient Call Agent backend")
    parser.add_argument("--replay", nargs=2, metavar=("MIC_WAV", "SYSTEM_WAV"), help="Replay mode WAV paths")
    parser.add_argument("--live", action="store_true", help="Live mic/system audio (disables replay)")
    parser.add_argument("--host", default=settings.backend_host)
    parser.add_argument("--port", type=int, default=settings.backend_port)
    args = parser.parse_args()

    if args.replay:
        settings.replay_mode = True
        settings.replay_mic_wav = args.replay[0]
        settings.replay_system_wav = args.replay[1]
    elif args.live:
        settings.replay_mode = False

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
