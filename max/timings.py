"""
Pure-Python timing buffer for Max's latency instrumentation.

No Pipecat imports, no FastAPI imports — keeps this unit-testable without
booting the whole stack.

A "turn" is one user-utterance → Max-response cycle.  It opens when the
pipeline sees UserStoppedSpeakingFrame (end of user speech) and closes
when the bridge forwards the first real TTS audio chunk to MBaaS.
"""
from __future__ import annotations

import time
from typing import Callable, Optional


# Markers recorded per turn, in expected chronological order.  Not strictly
# required by record() (which accepts any marker), kept for documentation.
MARKERS = (
    "T_speech_end",
    "T_stt_final",
    "T_llm_response_start",
    "T_llm_first_token",
    "T_llm_response_end",
    "T_tts_started",
    "T_tts_first_audio",
    "T_mbass_first_send",
)


class TimingBuffer:
    """In-memory ring buffer of per-turn timing markers.

    Parameters
    ----------
    clock : Callable[[], float]
        Monotonic clock.  Production code passes ``time.monotonic``;
        tests pass a deterministic fake.
    max_turns : int
        Maximum completed turns to retain.  Older turns are dropped.
    orphan_timeout_s : float
        If a turn stays open longer than this (no T_mbass_first_send),
        it's force-closed with ``incomplete=True``.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        max_turns: int = 20,
        orphan_timeout_s: float = 10.0,
    ) -> None:
        self._clock = clock
        self._max_turns = max_turns
        self._orphan_timeout_s = orphan_timeout_s
        self._next_id = 0
        self.current: Optional[dict] = None
        self.completed: list[dict] = []
        # Tool calls captured during the in-flight turn.
        self._tool_calls: list[dict] = []

    def open_turn(self) -> int:
        """Start a new turn.  Closes any orphaned previous turn first.

        Returns the new turn_id.
        """
        self._maybe_close_orphan()
        self._next_id += 1
        self.current = {
            "turn_id": self._next_id,
            "T_speech_end": self._clock(),
        }
        self._tool_calls = []
        return self._next_id

    def record(self, marker: str) -> None:
        """Record a timing marker on the current turn.

        Silently no-ops if no turn is open or the marker is already set
        (first sighting wins — we want first-token, not last-token).
        """
        if self.current is None:
            return
        key = marker if marker.startswith("T_") else f"T_{marker}"
        if key in self.current:
            return  # first sighting wins
        self.current[key] = self._clock()

    def record_tool(self, name: str, duration_ms: float) -> None:
        """Record a completed tool call on the current turn."""
        if self.current is None:
            return
        self._tool_calls.append({"name": name, "duration_ms": round(duration_ms, 1)})

    def close_turn(self, transcript: str = "") -> Optional[dict]:
        """Mark T_mbass_first_send and move the turn to ``completed``.

        Returns the completed turn dict, or None if no turn was open.
        """
        if self.current is None:
            return None
        self.current["T_mbass_first_send"] = self._clock()
        self.current["transcript"] = transcript
        self.current["tool_calls"] = list(self._tool_calls)
        self._finalize(self.current, incomplete=False)
        done = self.current
        self.current = None
        self._tool_calls = []
        return done

    def snapshot(self) -> list[dict]:
        """Return a copy of completed turns, newest last.

        Force-closes any hanging orphan first so /debug/timings surfaces it.
        """
        self._maybe_close_orphan()
        return list(self.completed)

    # ── internals ────────────────────────────────────────────────────────────

    def _maybe_close_orphan(self) -> None:
        if self.current is None:
            return
        age = self._clock() - self.current.get("T_speech_end", self._clock())
        if age < self._orphan_timeout_s:
            return
        self.current["tool_calls"] = list(self._tool_calls)
        self._finalize(self.current, incomplete=True)
        self.current = None
        self._tool_calls = []

    def _finalize(self, turn: dict, *, incomplete: bool) -> None:
        turn["incomplete"] = incomplete
        turn["deltas_ms"] = compute_deltas(turn)
        self.completed.append(turn)
        while len(self.completed) > self._max_turns:
            self.completed.pop(0)


def compute_deltas(turn: dict) -> dict:
    """Compute per-stage deltas in milliseconds from a raw turn dict.

    Uses T_speech_end as the anchor.  Any marker absent from the turn
    yields a missing key in the output — callers should not assume
    all stages fired.
    """
    if turn.get("T_speech_end") is None:
        return {}
    out: dict = {}
    pairs = [
        ("stt_wait_ms",    "T_speech_end",         "T_stt_final"),
        ("llm_prefill_ms", "T_stt_final",          "T_llm_first_token"),
        ("llm_total_ms",   "T_llm_response_start", "T_llm_response_end"),
        ("tts_wait_ms",    "T_llm_response_end",   "T_tts_first_audio"),
        ("bridge_wait_ms", "T_tts_first_audio",    "T_mbass_first_send"),
        ("total_ms",       "T_speech_end",         "T_mbass_first_send"),
    ]
    for label, start_key, end_key in pairs:
        start = turn.get(start_key)
        end = turn.get(end_key)
        if start is None or end is None:
            continue
        out[label] = round((end - start) * 1000.0, 1)
    return out
