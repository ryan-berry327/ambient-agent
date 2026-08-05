"""Session cost tracking."""

from __future__ import annotations

# Rough pricing estimates (USD)
DEEPGRAM_COST_PER_MINUTE = 0.0043  # Nova-2 streaming approx
HAIKU_INPUT_COST_PER_M = 1.0
HAIKU_OUTPUT_COST_PER_M = 5.0


def estimate_cost_usd(deepgram_minutes: float, input_tokens: int, output_tokens: int) -> float:
    dg = deepgram_minutes * DEEPGRAM_COST_PER_MINUTE
    haiku = (input_tokens / 1_000_000 * HAIKU_INPUT_COST_PER_M) + (
        output_tokens / 1_000_000 * HAIKU_OUTPUT_COST_PER_M
    )
    return round(dg + haiku, 4)
