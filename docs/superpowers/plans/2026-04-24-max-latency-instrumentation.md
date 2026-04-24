# Max Latency Instrumentation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add passive, behavior-preserving timing instrumentation to Max's Pipecat pipeline so we can attribute the 4–5 s response latency to a specific stage (VAD / STT / LLM / tool / TTS / bridge), then expose per-turn breakdowns via a new `/debug/timings` endpoint.

**Architecture:** One `TimingTap` Pipecat `FrameProcessor` placed near the end of the pipeline (just before `transport.output`) passively observes frames — `UserStoppedSpeakingFrame`, `LLMFullResponseStartFrame`, first `LLMTextFrame`, `FunctionCallInProgressFrame`, `FunctionCallResultFrame`, `TTSStartedFrame`, first `TTSAudioRawFrame`. Each sighting updates a `current_turn` dict keyed by a monotonic `turn_id`. The `ws_pipecat` bridge records `T_mbass_first_send` when it forwards the first real audio chunk to MBaaS for that turn. Tool functions are wrapped with a timing decorator. A new `/debug/timings` endpoint returns the last 20 completed turns. All additions are behind a module-level buffer; removing the buffer removes the feature.

**Tech Stack:** Python 3.11, Pipecat 0.0.108, FastAPI, `time.monotonic()` for all timing, `pytest` for unit tests on the pure-logic pieces.

**Resolved open questions from the spec:**
- Pipecat 0.0.108 exposes all required frames: `UserStoppedSpeakingFrame` (frames.py:1204), `LLMFullResponseStartFrame` (1995), `LLMTextFrame` (395), `FunctionCallInProgressFrame` (2100), `FunctionCallResultFrame` (912), `TTSStartedFrame` (2139), `TTSAudioRawFrame` (293). No event-handler workaround needed — a single `FrameProcessor` tap catches them all.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `max/timings.py` | Pure logic — state container, delta computation, orphan cleanup, JSON shaping. No Pipecat imports. Pure-Python. | Create |
| `max/server.py` | Wire `TimingTap` into pipeline; extend `ws_pipecat` bridge; wrap tool functions; add `/debug/timings` endpoint. | Modify |
| `tests/__init__.py` | Empty — marks tests package. | Create |
| `tests/test_timings.py` | Unit tests for `max/timings.py` pure logic (delta math, orphan cleanup, JSON shape). | Create |
| `requirements-dev.txt` | `pytest`. Dev-only; not shipped to Railway. | Create |
| `docs/superpowers/specs/2026-04-24-max-latency-instrumentation-design.md` | Existing spec — no change. | Leave |

**Why a separate `max/timings.py`:** Keeps state + pure logic unit-testable without booting Pipecat or FastAPI. `server.py` just imports and calls it. `server.py` is already 933 lines; we do not want to grow it with untested logic.

**Branch:** All work on `instrumentation/latency-diag`, off `main`.

---

## Task 1: Create branch and test scaffolding

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_timings.py` (empty placeholder; filled in Task 2)
- Create: `requirements-dev.txt`

- [ ] **Step 1: Create the branch**

Run:
```bash
cd ~/Documents/Claude/Max\ AI\ Employee/
git checkout -b instrumentation/latency-diag
```
Expected: `Switched to a new branch 'instrumentation/latency-diag'`

- [ ] **Step 2: Verify the existing spec is on this branch**

Run: `ls docs/superpowers/specs/`
Expected: Shows `2026-04-24-max-latency-instrumentation-design.md` in the file list.

- [ ] **Step 3: Create `requirements-dev.txt`**

Contents:
```
pytest>=8.0.0
```

- [ ] **Step 4: Create `tests/__init__.py`**

Leave it empty (zero bytes).

- [ ] **Step 5: Create `tests/test_timings.py` placeholder**

Contents:
```python
"""Unit tests for max/timings.py — pure logic only, no Pipecat or FastAPI."""
```

- [ ] **Step 6: Install pytest in the venv**

Run: `./venv/bin/pip install -r requirements-dev.txt`
Expected: `Successfully installed pytest-...`

- [ ] **Step 7: Verify pytest runs**

Run: `./venv/bin/pytest tests/ -v`
Expected: `collected 0 items` — zero failures, zero passes, zero errors.

- [ ] **Step 8: Commit**

```bash
git add tests/__init__.py tests/test_timings.py requirements-dev.txt
git commit -m "test: scaffold pytest for instrumentation work"
```

---

## Task 2: Write `max/timings.py` — state and pure logic (TDD)

**Files:**
- Create: `max/timings.py`
- Modify: `tests/test_timings.py`

This task covers the pure-Python logic: opening a turn, recording markers, computing deltas, closing a turn, orphan timeout, and JSON shaping. Every behavior gets a failing test first.

### Step 1: Write test for `open_turn` and `record`

- [ ] Replace `tests/test_timings.py` contents with:

```python
"""Unit tests for max/timings.py — pure logic only, no Pipecat or FastAPI."""
import pytest
from max.timings import TimingBuffer


def test_open_turn_assigns_sequential_ids():
    buf = TimingBuffer(clock=lambda: 0.0)
    t1 = buf.open_turn()
    t2 = buf.open_turn()
    assert t1 == 1
    assert t2 == 2


def test_record_stores_marker_on_current_turn():
    clock_values = iter([100.0, 100.18])
    buf = TimingBuffer(clock=lambda: next(clock_values))
    buf.open_turn()                 # clock reads 100.0 → T_speech_end = 100.0
    buf.record("stt_final")         # clock reads 100.18 → recorded as 100.18
    assert buf.current["T_speech_end"] == 100.0
    assert buf.current["T_stt_final"] == 100.18
```

### Step 2: Run — expect failure

- [ ] Run: `./venv/bin/pytest tests/test_timings.py -v`
- [ ] Expected: `ModuleNotFoundError: No module named 'max.timings'`

### Step 3: Create `max/timings.py` with the minimal implementation

- [ ] Contents:

```python
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


# Markers recorded per turn, in expected chronological order.
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
        key = f"T_{marker}" if not marker.startswith("T_") else marker
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
        """Return a copy of completed turns, newest last."""
        # Force-close any hanging orphan so /debug/timings surfaces it.
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
    anchor = turn.get("T_speech_end")
    if anchor is None:
        return {}
    out: dict = {}
    pairs = [
        ("stt_wait_ms",        "T_speech_end",        "T_stt_final"),
        ("llm_prefill_ms",     "T_stt_final",        "T_llm_first_token"),
        ("llm_total_ms",       "T_llm_response_start","T_llm_response_end"),
        ("tts_wait_ms",        "T_llm_response_end", "T_tts_first_audio"),
        ("bridge_wait_ms",     "T_tts_first_audio",  "T_mbass_first_send"),
        ("total_ms",           "T_speech_end",        "T_mbass_first_send"),
    ]
    for label, start_key, end_key in pairs:
        start = turn.get(start_key)
        end = turn.get(end_key)
        if start is None or end is None:
            continue
        out[label] = round((end - start) * 1000.0, 1)
    return out
```

### Step 4: Run tests — should pass

- [ ] Run: `./venv/bin/pytest tests/test_timings.py -v`
- [ ] Expected: Both tests pass:
  ```
  tests/test_timings.py::test_open_turn_assigns_sequential_ids PASSED
  tests/test_timings.py::test_record_stores_marker_on_current_turn PASSED
  ```

### Step 5: Add tests for `close_turn`, orphan cleanup, and delta computation

- [ ] Append to `tests/test_timings.py`:

```python
def test_close_turn_computes_deltas_and_returns_turn():
    clock_values = iter([100.0, 100.18, 100.80, 102.04, 102.14])
    buf = TimingBuffer(clock=lambda: next(clock_values))
    buf.open_turn()                       # 100.0 — T_speech_end
    buf.record("stt_final")               # 100.18
    buf.record("llm_first_token")         # 100.80
    buf.record("tts_first_audio")         # 102.04
    done = buf.close_turn(transcript="hey max")   # 102.14 → T_mbass_first_send
    assert done["turn_id"] == 1
    assert done["transcript"] == "hey max"
    assert done["incomplete"] is False
    d = done["deltas_ms"]
    assert d["stt_wait_ms"] == 180.0           # 100.18 - 100.0
    assert d["bridge_wait_ms"] == 100.0        # 102.14 - 102.04
    assert d["total_ms"] == 2140.0             # 102.14 - 100.0


def test_orphan_turn_is_force_closed_on_next_open():
    # Turn opens at t=0, never closes.  Next open at t=15 → orphan closed.
    ticks = iter([0.0, 15.0, 15.0, 15.0])   # open_turn, maybe_close_orphan, open_turn body
    buf = TimingBuffer(clock=lambda: next(ticks), orphan_timeout_s=10.0)
    buf.open_turn()                     # t=0
    buf.open_turn()                     # triggers orphan close of turn 1, opens turn 2
    completed = buf.completed
    assert len(completed) == 1
    assert completed[0]["turn_id"] == 1
    assert completed[0]["incomplete"] is True


def test_first_sighting_wins_for_repeated_markers():
    ticks = iter([100.0, 100.1, 100.2])
    buf = TimingBuffer(clock=lambda: next(ticks))
    buf.open_turn()
    buf.record("llm_first_token")      # 100.1
    buf.record("llm_first_token")      # 100.2 — ignored
    assert buf.current["T_llm_first_token"] == 100.1


def test_tool_calls_attach_to_current_turn():
    buf = TimingBuffer(clock=lambda: 0.0)
    buf.open_turn()
    buf.record_tool("get_testing_tickets", 312.4)
    buf.record_tool("log_task", 42.0)
    done = buf.close_turn()
    assert done["tool_calls"] == [
        {"name": "get_testing_tickets", "duration_ms": 312.4},
        {"name": "log_task", "duration_ms": 42.0},
    ]


def test_max_turns_evicts_oldest():
    buf = TimingBuffer(clock=lambda: 0.0, max_turns=2)
    for _ in range(3):
        buf.open_turn()
        buf.close_turn()
    assert [t["turn_id"] for t in buf.completed] == [2, 3]


def test_snapshot_on_dangling_turn_surfaces_orphan():
    ticks = iter([0.0, 11.0, 11.0])
    buf = TimingBuffer(clock=lambda: next(ticks), orphan_timeout_s=10.0)
    buf.open_turn()                # t=0
    snap = buf.snapshot()          # t=11 → orphan close
    assert len(snap) == 1
    assert snap[0]["incomplete"] is True
```

### Step 6: Run tests — all must pass

- [ ] Run: `./venv/bin/pytest tests/test_timings.py -v`
- [ ] Expected: All 7 tests pass (`2 existing + 5 new + 1 first-sighting = 8 total` — count should match the number of test functions).

### Step 7: Commit

- [ ] Run:
```bash
git add max/timings.py tests/test_timings.py
git commit -m "feat(instrumentation): add pure-logic TimingBuffer with unit tests"
```

---

## Task 3: Add tool-call timing wrapper

**Files:**
- Modify: `max/server.py` — wrap the six `tool_*` functions registered on the LLM.

- [ ] **Step 1: Read current tool registration block**

Open `max/server.py` and locate lines around 369–428 (the `async def tool_*` functions through the `llm.register_function(...)` calls). Confirm there are six: `tool_get_jira_ticket`, `tool_get_testing_tickets`, `tool_log_task`, `tool_get_test_results`, `tool_get_standup_briefing`, `tool_save_standup_note`.

- [ ] **Step 2: Add import at the top of `_run_pipecat_pipeline_inner`**

Immediately after the existing `from max.persona import SYSTEM_PROMPT` line (around line 296), add:

```python
        from max.timings import TimingBuffer
```

- [ ] **Step 3: Add a module-level buffer near the other state**

In `max/server.py` under the `# ── In-memory state ──` section (around line 86–97), add:

```python
from max.timings import TimingBuffer
timings: TimingBuffer = TimingBuffer()  # singleton; one Max at a time
```

Place this after `last_real_audio_time` so all instrumentation state is co-located.

- [ ] **Step 4: Add a tool-timing decorator just before the `async def tool_*` block**

Insert this block in `_run_pipecat_pipeline_inner`, right after the `llm` is constructed and before `async def tool_get_jira_ticket`:

```python
    # ── Tool timing wrapper ──
    def _timed(name: str, fn):
        """Wrap a tool function to record its duration into the active turn."""
        async def _wrapper(params):
            t0 = time.monotonic()
            try:
                return await fn(params)
            finally:
                timings.record_tool(name, (time.monotonic() - t0) * 1000.0)
        _wrapper.__name__ = fn.__name__
        return _wrapper
```

- [ ] **Step 5: Update the six `llm.register_function(...)` calls to wrap each tool**

Replace:
```python
    llm.register_function("get_jira_ticket", tool_get_jira_ticket)
    llm.register_function("get_testing_tickets", tool_get_testing_tickets)
    llm.register_function("log_task", tool_log_task)
    llm.register_function("get_test_results", tool_get_test_results)
    llm.register_function("get_standup_briefing", tool_get_standup_briefing)
    llm.register_function("save_standup_note", tool_save_standup_note)
```

With:
```python
    llm.register_function("get_jira_ticket",       _timed("get_jira_ticket",       tool_get_jira_ticket))
    llm.register_function("get_testing_tickets",   _timed("get_testing_tickets",   tool_get_testing_tickets))
    llm.register_function("log_task",              _timed("log_task",              tool_log_task))
    llm.register_function("get_test_results",      _timed("get_test_results",      tool_get_test_results))
    llm.register_function("get_standup_briefing",  _timed("get_standup_briefing",  tool_get_standup_briefing))
    llm.register_function("save_standup_note",     _timed("save_standup_note",     tool_save_standup_note))
```

- [ ] **Step 6: Syntax check**

Run: `./venv/bin/python -c "import ast; ast.parse(open('max/server.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add max/server.py
git commit -m "feat(instrumentation): time each tool call into TimingBuffer"
```

---

## Task 4: Add `TimingTap` FrameProcessor

**Files:**
- Modify: `max/server.py` — add a new `TimingTap` class inside `_run_pipecat_pipeline_inner`.

- [ ] **Step 1: Add the TimingTap class after `SilenceTextFilter`**

In `max/server.py`, locate the `class SilenceTextFilter(FrameProcessor):` block (around line 308–319). Immediately after that class definition, add:

```python
    # ── Timing tap: passive observer, records frame timestamps ──
    # Placed LATE in the pipeline (just before transport.output).  Every frame
    # emitted anywhere upstream flows through this tap on its way out, so one
    # instance suffices — no need for multiple taps.
    #
    # CRITICAL: must call `await super().process_frame(frame, direction)` FIRST,
    # then pass the frame through via `push_frame`.  Skipping super() stalls
    # the pipeline on StartFrame (same bug that killed DiagLogger and the
    # original SilenceTextFilter — see memory/projects/max-ai-employee.md).
    class TimingTap(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            try:
                from pipecat.frames.frames import (
                    UserStoppedSpeakingFrame,
                    LLMFullResponseStartFrame,
                    LLMTextFrame,
                    LLMFullResponseEndFrame,
                    TTSStartedFrame,
                    TTSAudioRawFrame,
                )
                if isinstance(frame, UserStoppedSpeakingFrame):
                    tid = timings.open_turn()
                    alog(f"TIMING turn={tid} speech_end")
                elif isinstance(frame, LLMFullResponseStartFrame):
                    timings.record("llm_response_start")
                elif isinstance(frame, LLMTextFrame):
                    # First LLMTextFrame of the turn = time-to-first-token.
                    # record() is first-sighting-wins, so repeats are free.
                    timings.record("llm_first_token")
                elif isinstance(frame, LLMFullResponseEndFrame):
                    timings.record("llm_response_end")
                elif isinstance(frame, TTSStartedFrame):
                    timings.record("tts_started")
                elif isinstance(frame, TTSAudioRawFrame):
                    timings.record("tts_first_audio")
            except Exception as e:
                # Instrumentation must NEVER break the pipeline.
                alog(f"TIMING TAP EXC: {e}")
            await self.push_frame(frame, direction)
```

Note: We import frame classes inside `process_frame` (not at module top) to match the existing codebase's lazy-import pattern for Pipecat types and keep startup robust against version drift.

- [ ] **Step 2: Instantiate the tap right after the existing `silence_filter = SilenceTextFilter()` line**

Locate (around line 489):
```python
    # ── Silence filter instance ──
    silence_filter = SilenceTextFilter()
```

Immediately after it, add:
```python
    timing_tap = TimingTap()
```

- [ ] **Step 3: Wire the tap into the pipeline**

Locate the pipeline definition (around lines 502–512):
```python
    # ── Pipeline ──
    pipeline = Pipeline([
        transport.input(),
        stt,
        aggregator_pair.user(),
        llm,
        silence_filter,   # drops "..." before TTS sees it
        tts,
        aggregator_pair.assistant(),
        transport.output(),
    ])
    alog("PIPELINE built: transport→STT→Claude→filter→TTS→transport")
```

Replace with:
```python
    # ── Pipeline ──
    pipeline = Pipeline([
        transport.input(),
        stt,
        aggregator_pair.user(),
        llm,
        silence_filter,   # drops "..." before TTS sees it
        tts,
        aggregator_pair.assistant(),
        timing_tap,       # passive timing observer — no frame mutation
        transport.output(),
    ])
    alog("PIPELINE built: transport→STT→Claude→filter→TTS→tap→transport")
```

- [ ] **Step 4: Syntax check**

Run: `./venv/bin/python -c "import ast; ast.parse(open('max/server.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add max/server.py
git commit -m "feat(instrumentation): add passive TimingTap FrameProcessor"
```

---

## Task 5: Record T_stt_final and wire the STT side

**Files:**
- Modify: `max/server.py` — extend the existing `@stt.event_handler("on_transcription")`.

- [ ] **Step 1: Find the existing handler (around line 497–500):**

```python
    @stt.event_handler("on_transcription")
    async def _on_stt(processor, frame):
        if hasattr(frame, 'text') and frame.text:
            alog(f"STT TRANSCRIPT: \"{frame.text}\"")
```

- [ ] **Step 2: Add a module-level `last_transcript` dict in the state section**

The `ws_pipecat` handler (defined at module scope) needs to read the final transcript when it closes a turn. Because the STT handler lives inside `_run_pipecat_pipeline_inner`, we publish via a module-level dict keyed by `bot_id`.

In `max/server.py` under the `# ── In-memory state ──` section, next to `last_real_audio_time` and `timings`, add:
```python
last_transcript: dict[str, str] = {}  # bot_id → latest finalized transcript
```

- [ ] **Step 3: Extend the STT handler to record the marker and publish the transcript**

Replace the existing handler body with:
```python
    @stt.event_handler("on_transcription")
    async def _on_stt(processor, frame):
        if hasattr(frame, 'text') and frame.text:
            alog(f"STT TRANSCRIPT: \"{frame.text}\"")
            timings.record("stt_final")
            last_transcript[bot_id] = frame.text
```

(`bot_id` is in the closure of `_run_pipecat_pipeline_inner`.)

- [ ] **Step 4: Syntax check**

Run: `./venv/bin/python -c "import ast; ast.parse(open('max/server.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add max/server.py
git commit -m "feat(instrumentation): record T_stt_final and stash transcript per bot"
```

---

## Task 6: Record T_mbass_first_send in the `ws_pipecat` bridge

**Files:**
- Modify: `max/server.py` — `ws_pipecat` handler closes the turn on first real audio forwarded per turn.

The bridge currently forwards every audio chunk and updates `last_real_audio_time` for the silence-sender's benefit. We want to close the timing turn on the FIRST chunk forwarded after each `T_speech_end`. To know "first", we track whether the current turn has already been closed.

- [ ] **Step 1: Locate the `ws_pipecat` handler (around lines 667–710)**

Current audio-forwarding loop:
```python
            if "bytes" in msg and msg["bytes"]:
                try:
                    audio = protobuf_to_raw(msg["bytes"])
                    if audio:
                        audio_chunks_out += 1
                        # Update timestamp so silence sender yields to real TTS
                        last_real_audio_time[bot_id] = time.time()
                        if audio_chunks_out <= 3 or audio_chunks_out % 100 == 0:
                            alog(f"BRIDGE Pipecat→MBaaS: chunk #{audio_chunks_out} ({len(audio)} bytes)")
                        client_ws = client_connections.get(bot_id)
                        if client_ws:
                            await client_ws.send_bytes(audio)
                        else:
                            alog(f"BRIDGE Pipecat→MBaaS: no MBaaS connection")
```

- [ ] **Step 2: Replace the block with a version that closes the active turn on first audio**

```python
            if "bytes" in msg and msg["bytes"]:
                try:
                    audio = protobuf_to_raw(msg["bytes"])
                    if audio:
                        audio_chunks_out += 1
                        # Update timestamp so silence sender yields to real TTS
                        last_real_audio_time[bot_id] = time.time()
                        # TIMING: close the active turn on the first real audio
                        # chunk after a new T_speech_end.  timings.close_turn()
                        # is a no-op if no turn is currently open, so subsequent
                        # chunks within the same turn are free.
                        if timings.current is not None:
                            done = timings.close_turn(transcript=last_transcript.get(bot_id, ""))
                            if done:
                                d = done.get("deltas_ms", {})
                                alog(
                                    f"TIMING turn={done['turn_id']} total={d.get('total_ms','?')}ms "
                                    f"stt={d.get('stt_wait_ms','?')} "
                                    f"llm_prefill={d.get('llm_prefill_ms','?')} "
                                    f"tts_wait={d.get('tts_wait_ms','?')} "
                                    f"bridge_wait={d.get('bridge_wait_ms','?')} "
                                    f"tools={len(done.get('tool_calls', []))}"
                                )
                        if audio_chunks_out <= 3 or audio_chunks_out % 100 == 0:
                            alog(f"BRIDGE Pipecat→MBaaS: chunk #{audio_chunks_out} ({len(audio)} bytes)")
                        client_ws = client_connections.get(bot_id)
                        if client_ws:
                            await client_ws.send_bytes(audio)
                        else:
                            alog(f"BRIDGE Pipecat→MBaaS: no MBaaS connection")
```

- [ ] **Step 3: Syntax check**

Run: `./venv/bin/python -c "import ast; ast.parse(open('max/server.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add max/server.py
git commit -m "feat(instrumentation): close timing turn on first MBaaS audio send"
```

---

## Task 7: Add `/debug/timings` endpoint

**Files:**
- Modify: `max/server.py` — new FastAPI endpoint.

- [ ] **Step 1: Locate the existing `/debug` endpoint (around lines 899–919)**

- [ ] **Step 2: Add the new endpoint immediately after `/debug`**

```python
@app.get("/debug/timings")
async def debug_timings():
    """Per-turn latency breakdown.  Newest turns last.

    ``deltas_ms`` is the easy-to-read breakdown; raw ``T_*`` timestamps
    are included for anyone wanting to sanity-check the math.
    """
    turns = timings.snapshot()
    # Strip the raw T_* timestamps from the returned view — they're monotonic
    # seconds and not human-readable.  Keep deltas_ms + turn_id + transcript
    # + tool_calls + incomplete flag.
    view = []
    for t in turns:
        view.append({
            "turn_id":    t.get("turn_id"),
            "transcript": t.get("transcript", ""),
            "tool_calls": t.get("tool_calls", []),
            "deltas_ms":  t.get("deltas_ms", {}),
            "incomplete": t.get("incomplete", False),
        })
    return {
        "count":   len(view),
        "turns":   view,
        "as_of":   time.strftime("%Y-%m-%d %H:%M:%S IST"),
    }
```

- [ ] **Step 3: Syntax check**

Run: `./venv/bin/python -c "import ast; ast.parse(open('max/server.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 4: Smoke test the import path**

Run: `./venv/bin/python -c "from max.server import app, timings; print('imports OK', type(timings).__name__)"`
Expected: `imports OK TimingBuffer`

- [ ] **Step 5: Commit**

```bash
git add max/server.py
git commit -m "feat(instrumentation): expose GET /debug/timings endpoint"
```

---

## Task 8: Re-run the full unit test suite

**Files:**
- None changed.

- [ ] **Step 1: Run all tests**

Run: `./venv/bin/pytest tests/ -v`
Expected: All `test_timings.py` tests pass. No failures.

- [ ] **Step 2: If anything fails, fix the mismatch**

The pure-logic unit tests in `test_timings.py` should not be affected by anything in `server.py`. If they fail, it means `timings.py` was edited — revert or correct the change.

---

## Task 9: Push branch and deploy to Railway

**Files:**
- None changed.

- [ ] **Step 1: Confirm all previous commits are on the branch**

Run: `git log --oneline main..instrumentation/latency-diag`
Expected: Roughly 6–8 commits — scaffold, timings.py, tool wrapper, TimingTap, STT marker, bridge close, /debug/timings.

- [ ] **Step 2: Push the branch**

Run: `git push -u origin instrumentation/latency-diag`
Expected: Branch pushed; GitHub URL returned.

- [ ] **Step 3: Wait for Railway auto-deploy if this branch is the deployed one**

Check Railway dashboard. Max Brain deploys from `main` by default. Two options:
- **Recommended**: temporarily change Railway's deploy branch to `instrumentation/latency-diag` for this diagnostic run only.
- **Alternative**: merge to main via fast-forward, deploy, then revert merge after diagnosis. (Risk: pollutes main history.)

Go with the recommended option — Suren updates Railway deploy branch manually.

- [ ] **Step 4: Verify deployment health**

After Railway reports deploy success (~2–3 min), run from the sandbox:
```bash
curl -s https://max-brain-production.up.railway.app/health
```
Expected: `{"status":"ok", "architecture":"pipecat-streaming", ...}`

- [ ] **Step 5: Verify new endpoint exists**

```bash
curl -s https://max-brain-production.up.railway.app/debug/timings
```
Expected: `{"count": 0, "turns": [], "as_of": "..."}`

---

## Task 10: Run the live TEST meeting

**Files:**
- None changed.

- [ ] **Step 1: Suren joins the TEST meeting**

URL: `https://meet.google.com/mmg-mjgn-njd`

⚠️ NEVER the real standup URL (`https://meet.google.com/agm-fhsi-khp`).

- [ ] **Step 2: Trigger Max to join**

Run:
```bash
curl -X POST https://max-brain-production.up.railway.app/join \
  -H "Content-Type: application/json" \
  -d '{"meeting_url":"https://meet.google.com/mmg-mjgn-njd","bot_name":"Max"}'
```
Expected: `{"ok":true, "bot_id":"...", "meeting_url":"..."}`

- [ ] **Step 3: Run ~8–10 turns, mixing tool-calling and simple responses**

Suggested turns:
1. "Hey Max, how's it going?" (simple greeting, no tool)
2. "Max, what's in testing?" (triggers `get_testing_tickets`)
3. "Max, lookup 1275" (triggers `get_jira_ticket`)
4. "Max, log ESB-1399 for me" (triggers `log_task`)
5. "Max, give me your update" (triggers `get_test_results`)
6. "Max, what's 2 plus 2?" (simple, no tool, testing latency floor)
7. "Max, take a note: Suren said finish testing by 3pm" (triggers `save_standup_note`)
8. "Thanks Max!" (simple, no tool)

- [ ] **Step 4: Pull timings after the meeting**

Run:
```bash
curl -s https://max-brain-production.up.railway.app/debug/timings | python3 -m json.tool
```
Expected: `count` between 5 and 10 with each turn showing `deltas_ms`.

- [ ] **Step 5: Also fetch `/debug` to cross-check the diag log**

Run:
```bash
curl -s https://max-brain-production.up.railway.app/debug | python3 -m json.tool
```
Look at the `diag_log` entries starting with `TIMING turn=...`. They should match the `deltas_ms` values from `/debug/timings`.

- [ ] **Step 6: Save the raw output**

Save the JSON to `docs/superpowers/specs/2026-04-24-max-latency-diagnostic-run.json` for the record.

---

## Task 11: Write findings note and transition to fix plan

**Files:**
- Create: `docs/superpowers/specs/2026-04-24-max-latency-findings.md`

- [ ] **Step 1: Analyze the data**

For each turn, look at which `deltas_ms` value is largest. Group by turn type:
- Simple no-tool turns — where's the time?
- Tool-calling turns — how much of the total is the tool round-trip (`llm_total_ms`) vs the pre-tool wait (`stt_wait_ms` + `llm_prefill_ms`)?

- [ ] **Step 2: Write the findings doc**

Use this outline:

```markdown
# Findings — Max Latency Diagnostic Run (2026-04-24)

**Baseline:** [count] turns observed, [range] total_ms.

## Per-stage breakdown (median across turns)
| Stage | Median ms | % of total |
|---|---|---|
| stt_wait | ... | ... |
| llm_prefill | ... | ... |
| llm_total | ... | ... |
| tts_wait | ... | ... |
| bridge_wait | ... | ... |

## The slow stage is: [X]

Evidence: [specific turn IDs and their breakdowns]

## Contributing factors ruled in / ruled out
- [x] or [ ] Silence sender interaction with Meet buffer
- [x] or [ ] SilenceTextFilter overhead
- [x] or [ ] System prompt prefill growth
- [x] or [ ] Deepgram default endpointing
- [x] or [ ] Tool-call double-round-trip
- [x] or [ ] Something else: ...

## Recommended fix options for the next spec

[2–3 options with trade-offs]
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-04-24-max-latency-findings.md docs/superpowers/specs/2026-04-24-max-latency-diagnostic-run.json
git commit -m "docs: findings from latency diagnostic run"
git push
```

- [ ] **Step 4: Hand back to Suren**

Present the findings. Propose the next spec scope. Await approval before writing the fix plan.

---

## Post-implementation: what to do with the instrumentation

Options after the diagnostic:
1. **Keep it** — it's cheap, low-risk, and useful for future regressions. Merge the branch to main.
2. **Keep only the logging, strip the endpoint** — minimal surface area kept around.
3. **Revert everything** — if the fix is obvious and we won't need this view again.

The findings doc should recommend one of these explicitly.

---

## Self-Review (Author's Own Check)

**Spec coverage:**
- Measure per-turn timestamps at VAD / STT / LLM-first-token / tool / TTS / bridge → covered by Tasks 3, 4, 5, 6.
- `/debug/timings` endpoint → Task 7.
- Orphan-turn handling → Task 2 logic + unit test.
- No pipeline behavior change → `TimingTap` calls `super().process_frame()` first and always `push_frame`s (Task 4); tool wrapper always `return await fn(params)` (Task 3); bridge only ADDS a close-turn call without altering the audio forward path (Task 6).
- Branch `instrumentation/latency-diag` → Task 1.
- Deploy to Railway + live TEST meeting run + findings doc → Tasks 9, 10, 11.
- Open questions from spec resolved in the header (Pipecat frame classes verified to exist in 0.0.108).

**Placeholder scan:** No "TBD", no "implement later", no "similar to Task N", no "add validation" — every step has concrete code or a concrete command.

**Type consistency:** `TimingBuffer` is the only new type; its interface is defined in Task 2 and used unchanged in Tasks 3, 6, 7 (`open_turn`, `record`, `record_tool`, `close_turn`, `snapshot`, `current`). `MARKERS` tuple is declared but not strictly required by `record()` (which accepts any marker name) — kept for documentation.

**Risk of instrumentation breaking the pipeline:** `TimingTap` swallows all exceptions with `try/except`. `record()` and `record_tool()` no-op when no turn is open. Orphan cleanup runs every time a new turn opens. Worst case, `/debug/timings` returns an empty or partial list — the pipeline keeps running.

**One known sharp edge:** there is only ONE global `TimingBuffer`. If two `ws_meetingbaas` handlers run concurrently (multiple simultaneous meetings), their turns would interleave in the same buffer. For the current Max use case (one meeting at a time), this is acceptable. If we ever run multiple meetings, bucket by `bot_id`.
