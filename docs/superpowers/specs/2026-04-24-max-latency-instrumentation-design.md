# Design — Max Latency Instrumentation (Diagnostic Only)

**Author:** Claude (QA / Virtual Agent context), collaborating with Suren
**Date:** 2026-04-24
**Status:** Approved (verbal) — awaiting written sign-off
**Repo:** `suren-pm/max-brain` (local at `~/Documents/Claude/Max AI Employee/`)
**Branch (planned):** `instrumentation/latency-diag`

---

## 1. Problem

Max's response time from "user stops talking" to "Max starts speaking" has regressed from **2–3 seconds** (pre-April-16 stable state, roughly commit `50ad248`) to **4–5 seconds** (current HEAD at commit `053c717`).

Every other behavior — tool use, persona, note-taking, greetings, joining reliably — is working as desired. The only issue is the ~2-second added latency.

## 2. Why this is a design doc and not just a fix

Static diff of `v1.0..HEAD` surfaces several plausible latency contributors (new silence sender, new text filter, longer system prompt, Deepgram endpointing possibly defaulting to slow, silence-sender ↔ Google Meet jitter-buffer interaction) but **none of them alone plausibly account for 2,000 ms**. Best-case static arithmetic lands around 150–630 ms.

Picking a "fix" without measurement would be guessing. This spec is therefore for a **measurement pass**, not a fix. A second spec will follow once the data tells us which stage owns the delay.

## 3. Goal

After this work, we can say definitively: "The ~2 s regression is in stage X" — where X is one of VAD turn-end detection, STT finalization, LLM time-to-first-token, tool-call round-trip, TTS time-to-first-audio, or transport/bridge/MBaaS send.

## 4. Non-goals

- Do NOT change any pipeline behavior. No frame mutation, no reordering, no new buffers.
- Do NOT attempt the fix in this spec. The fix lands in a follow-up spec after we read the data.
- Do NOT change tool use, persona, or any user-visible behavior.
- Do NOT touch any scheduled tasks, Slack integration, or endpoints unrelated to the pipeline.

## 5. What we measure

Six per-turn timestamps, all monotonic:

| Marker | Definition | Source |
|---|---|---|
| `T_speech_end` | Silero VAD declared end-of-user-speech | `UserStoppedSpeakingFrame` via a passive `FrameProcessor` tap |
| `T_stt_final` | Deepgram emitted final transcript | Existing `@stt.event_handler("on_transcription")` — extended |
| `T_llm_first_token` | Anthropic stream returned first token | `@llm.event_handler` on first output frame |
| `T_tool_calls` | List of `{name, duration_ms}` for any tools fired this turn | Wrap each registered `tool_*` function with a timing decorator |
| `T_tts_first_audio` | Deepgram TTS produced first audio frame | `@tts.event_handler` or first `AudioRawFrame` observed downstream of `tts` |
| `T_mbass_first_send` | Bridge forwarded first real audio chunk to MBaaS WebSocket | Existing `ws_pipecat` loop — extended |

Deltas computed per turn:

- `stt_wait` = `T_stt_final − T_speech_end`
- `llm_prefill` = `T_llm_first_token − T_stt_final`
- `llm_to_tts` = `T_tts_first_audio − T_llm_first_token` (incl. tool round-trip time)
- `tts_to_mbass` = `T_mbass_first_send − T_tts_first_audio`
- `total` = `T_mbass_first_send − T_speech_end`

## 6. Architecture (where it lives in the pipeline)

Current pipeline:

```
transport.input → stt → aggregator.user → llm → silence_filter → tts → aggregator.assistant → transport.output
```

We add one passive tap immediately after `transport.input`:

```
transport.input → [TimingTap] → stt → aggregator.user → llm → silence_filter → tts → aggregator.assistant → transport.output
```

`TimingTap` is a `FrameProcessor` whose ONLY job is to observe `UserStoppedSpeakingFrame`, record the timestamp, and pass every frame through unmodified. It MUST call `await super().process_frame(...)` first — same StartFrame discipline that bit the DiagLogger and the original SilenceTextFilter per the April dev memory.

All other markers are captured via Pipecat's existing event-handler API (no FrameProcessors).

## 7. State

New module-level state in `server.py`:

```python
turn_timings: list[dict] = []  # capped at 50 turns
current_turn: dict = {}        # accumulates markers for the in-flight turn
turn_counter: int = 0
```

When `T_speech_end` fires, a new `current_turn` dict is opened with a fresh `turn_id`. Subsequent markers append to it. When `T_mbass_first_send` fires, the turn is closed, deltas computed, appended to `turn_timings`, and `current_turn` reset.

Edge case: if a turn is orphaned (user speaks but no response ever reaches MBaaS), after 10 seconds it is closed with `incomplete=true` and appended anyway. This prevents forever-leaking in-flight state.

## 8. Exposure

New endpoint:

```
GET /debug/timings
```

Returns:

```json
{
  "count": 5,
  "turns": [
    {
      "turn_id": 3,
      "transcript": "hey max what's in testing",
      "speech_end": "14:32:08.412",
      "stt_final_ms": 180,
      "llm_prefill_ms": 620,
      "llm_to_tts_ms": 1240,
      "tts_to_mbass_ms": 95,
      "tool_calls": [{"name": "get_testing_tickets", "duration_ms": 312}],
      "total_ms": 2135
    },
    ...
  ],
  "as_of": "2026-04-24 14:35:02 IST"
}
```

Also include a one-line summary per turn in the existing `diag_log` so it shows up in `/debug` alongside other events.

## 9. Risk

- **Observer effect:** Event handlers and a single passive FrameProcessor have microsecond-level overhead. Not load-bearing on a 2-s regression.
- **StartFrame bug regression:** The `TimingTap` FrameProcessor has exactly the same failure mode as the original SilenceTextFilter and DiagLogger — if `super().process_frame()` is omitted, the pipeline stalls at boot. Mitigation: it's the first thing the implementation checks, it's called out in a code comment, and there's a sanity log line ("TimingTap: StartFrame received") to confirm startup.
- **Event handler compatibility:** Pipecat 0.0.108's event API for LLM/TTS first-token events needs verification at implementation time. If a handler isn't directly available, we fall back to a passive tap FrameProcessor at that point in the pipeline. Implementation spec will nail this down before coding.

## 10. Deliverables

1. New branch `instrumentation/latency-diag` off main.
2. Patch to `max/server.py` (~60–100 lines: TimingTap class, state buffer, event handler extensions, tool wrapper, `/debug/timings` endpoint).
3. This design doc committed under `docs/superpowers/specs/`.
4. Deploy to Railway (auto-deploy on push).
5. One test-meeting run in the TEST meeting URL (`https://meet.google.com/mmg-mjgn-njd`) — NEVER the real standup. Speak ~5–10 turns to generate samples.
6. Read `/debug/timings` output, identify the slow stage, write a short findings note in the same `docs/superpowers/specs/` directory.

## 11. Success criteria

- Every turn in a test meeting produces a complete row in `/debug/timings`.
- At least one slow turn (≥3.5 s total) shows a breakdown pointing at a specific stage.
- We can name the bottleneck stage with evidence, not theory.
- Pipeline behavior before and after the patch is observably identical (no new silences, no cut-offs, no voice break-ups, no failed joins).

## 12. Rollback

If anything misbehaves, revert the branch. No production data, no schema changes, no external integration changes.

## 13. What happens after

A follow-up spec (`max-latency-fix-design.md`, dated when we get there) scoped to whatever stage the data points at. That spec will propose 2–3 targeted fixes with trade-offs and get its own approval gate before any production-behavior code is changed.

---

## Open questions

1. Does Pipecat 0.0.108 expose a first-token event on `AnthropicLLMService`? If not, we insert a passive tap `FrameProcessor` between `llm` and `silence_filter` to catch the first `TextFrame`. To be resolved at implementation time via Pipecat source inspection.
2. Same question for `DeepgramTTSService` first-audio. Fallback is a passive tap between `silence_filter` and `tts` output.
