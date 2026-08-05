"""Session cost tracking (Cursor-only stack)."""

from __future__ import annotations

# deepgram_minutes column stores audio minutes processed locally (free)
# haiku_* columns store estimated Cursor token usage from distiller runs
CURSOR_COST_PER_M_TOKENS = 2.0  # rough blended estimate


def estimate_cost_usd(deepgram_minutes: float, input_tokens: int, output_tokens: int) -> float:
    del deepgram_minutes  # local whisper is free
    tokens = input_tokens + output_tokens
    return round(tokens / 1_000_000 * CURSOR_COST_PER_M_TOKENS, 4)
