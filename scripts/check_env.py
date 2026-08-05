"""Verify required API keys exist in backend/.env or environment (values not printed)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
ENV = BACKEND / ".env"
EXAMPLE = BACKEND / ".env.example"

REQUIRED = ("DEEPGRAM_API_KEY", "ANTHROPIC_API_KEY")


def _load_env_file() -> dict[str, str]:
    if not ENV.exists():
        return {}
    values: dict[str, str] = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def main() -> int:
    file_vals = _load_env_file()
    missing = []
    for key in REQUIRED:
        val = file_vals.get(key) or os.environ.get(key, "")
        if not val or val.startswith("your_"):
            missing.append(key)

    if missing:
        print(f"HALT: Missing API keys for: {', '.join(missing)}")
        if not ENV.exists():
            print(f"Create {ENV} from {EXAMPLE.name} or export keys in the environment.")
        return 1

    source = "backend/.env" if ENV.exists() else "environment variables"
    print(f"OK: DEEPGRAM_API_KEY and ANTHROPIC_API_KEY present ({source})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
