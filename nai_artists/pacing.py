"""Request spacing for the server queue (settings.toml [pacing]).

Requests are spaced out to keep load on NovelAI's servers low: after every generation the queue
waits a fixed `gap_ms` before it sends the next one, however many cells were clicked meanwhile.
"""

from __future__ import annotations

from .templates import Pacing


class Pacer:
    def __init__(self, pacing: Pacing):
        self.pacing = pacing

    def next_delay(self) -> tuple[float, str]:
        """Call once after each completed generation. Returns (seconds, reason)."""
        return self.pacing.gap_ms / 1000.0, "gap"
