"""Unit tests for max/timings.py — pure logic only, no Pipecat or FastAPI."""
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


def test_close_turn_computes_deltas_and_returns_turn():
    clock_values = iter([100.0, 100.18, 100.80, 102.04, 102.14])
    buf = TimingBuffer(clock=lambda: next(clock_values))
    buf.open_turn()                             # 100.0 — T_speech_end
    buf.record("stt_final")                     # 100.18
    buf.record("llm_first_token")               # 100.80
    buf.record("tts_first_audio")               # 102.04
    done = buf.close_turn(transcript="hey max") # 102.14 → T_mbass_first_send
    assert done["turn_id"] == 1
    assert done["transcript"] == "hey max"
    assert done["incomplete"] is False
    d = done["deltas_ms"]
    assert d["stt_wait_ms"] == 180.0            # 100.18 - 100.0
    assert d["bridge_wait_ms"] == 100.0         # 102.14 - 102.04
    assert d["total_ms"] == 2140.0              # 102.14 - 100.0


def test_orphan_turn_is_force_closed_on_next_open():
    ticks = iter([0.0, 15.0, 15.0, 15.0])
    buf = TimingBuffer(clock=lambda: next(ticks), orphan_timeout_s=10.0)
    buf.open_turn()                     # t=0
    buf.open_turn()                     # triggers orphan close of turn 1
    completed = buf.completed
    assert len(completed) == 1
    assert completed[0]["turn_id"] == 1
    assert completed[0]["incomplete"] is True


def test_open_turn_always_closes_prior_turn_even_if_under_timeout():
    # A fresh UserStoppedSpeakingFrame means the user has moved on — the
    # previous turn is finished whether or not it ever got a TTS response
    # (Max may have stayed silent).  Must not silently drop.
    ticks = iter([0.0, 2.0, 2.0, 2.0])   # only 2s gap — well under 10s
    buf = TimingBuffer(clock=lambda: next(ticks), orphan_timeout_s=10.0)
    buf.open_turn()                     # turn 1 at t=0
    buf.open_turn()                     # turn 2 at t=2 — turn 1 must be closed
    assert len(buf.completed) == 1
    assert buf.completed[0]["turn_id"] == 1
    assert buf.completed[0]["incomplete"] is True


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


def test_record_noop_when_no_turn_open():
    buf = TimingBuffer(clock=lambda: 0.0)
    buf.record("stt_final")        # must not raise
    buf.record_tool("something", 0)
    assert buf.current is None


def test_close_turn_returns_none_when_no_turn_open():
    buf = TimingBuffer(clock=lambda: 0.0)
    assert buf.close_turn() is None
