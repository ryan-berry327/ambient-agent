"""Quick Cursor REST API smoke test."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.services.cursor_api import CursorApiClient


def main() -> int:
    client = CursorApiClient()
    result = client.prompt('Return ONLY: {"changes":[],"spec":[]}')
    print("result:", result[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
