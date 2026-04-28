# Findings — Gemini 2.5 Flash LLM Swap Experiment

**Date:** 2026-04-28
**Branch:** `experiment/gemini-llm`
**Pushed to:** `github.com/suren-pm/max-brain`
**Status:** Experiment complete. Findings recorded. Railway flipped back to `main` (Claude Haiku 4.5) for production.

---

## 1. Goal

Test whether Max's persona, character, and operational behaviour transfer cleanly when the underlying LLM is swapped from **Anthropic Claude Haiku 4.5** to **Google Gemini 2.5 Flash**, with everything else held identical (same system prompt, same six tools, same Deepgram STT and TTS, same VAD, same pipeline structure, same voice — Aura Arcas).

---

## 2. What was changed

Diff against `main` (commit `70cfa23`):

| File | Change |
|---|---|
| `max/server.py` | `from pipecat.services.anthropic.llm import AnthropicLLMService` → `from pipecat.services.google.llm import GoogleLLMService`. Constructor swapped: `AnthropicLLMService(api_key=os.getenv("ANTHROPIC_API_KEY"), model="claude-haiku-4-5-20251001")` → `GoogleLLMService(api_key=os.getenv("GEMINI_API_KEY"), model="gemini-2.5-flash")`. |
| `requirements-server.txt` | Added `google` to the pipecat-ai extras: `pipecat-ai[anthropic,google,deepgram,silero,websocket]==0.0.108`. This pulls in `google-genai` which `GoogleLLMService` requires. |
| Railway env | Added `GEMINI_API_KEY` variable. Left in place after flip-back so the experiment branch can be redeployed without reconfiguring. |

Nothing else changed. Persona prompt is byte-identical. Tools, schemas, pipeline order, transport — all unchanged.

---

## 3. Live test setup

- TEST meeting URL: `https://meet.google.com/mmg-mjgn-njd`
- 30 turns observed via the permanent `/debug/timings` endpoint
- Mix of greetings, joke requests, off-topic chatter, ticket-related discussion

---

## 4. What worked — persona transferred in concept

- **Note-taking discipline:** Gemini-Max called `save_standup_note` reliably across the meeting. Captured the conversational flow accurately ("Asked Max to tell a joke", "Complimented Max's joke", "Said they were just kidding about asking for Australian news", etc.). 12+ notes captured in the session.
- **Tool calling:** Gemini called the right tool at the right time. Tool-call durations were ~0.9–1.5 ms (local in-memory tools), comparable to Claude.
- **Latency band:** Gemini hit a 540 ms turn at one point — faster than anything observed with Claude. Average per-turn latency was in the same range as Claude (1.0–2.0 s for simple turns, 1.7–2.1 s for tool-using turns).
- **Voice quality:** identical, because Deepgram TTS Aura Arcas is unchanged.
- **Personality flavour:** Gemini-Max made jokes when asked. Suren reported the responses felt "really good" in the lighter-touch parts of the conversation.

---

## 5. What did not transfer cleanly — vendor-specific behavioural drift

### 5.1 Over-eager response to non-Max chatter
**Observed:** Gemini-Max interjected with phrases like "Sounds like a good plan" and "That's fine with me" during team-internal discussion that was not directed at Max.

**Mechanism:** The persona's "When in doubt, RESPOND. Silence feels broken" rule is interpreted differently by Gemini vs Claude. Claude reads it conservatively — silence on background chatter is the default. Gemini reads it aggressively — its base temperament is "be helpful and present," so any ambiguous chatter gets read as addressed-at-Max.

**Implication:** The same prompt produces different baseline behaviour across vendors because each model has a different default temperament that bleeds through instruction-following.

### 5.2 Audio cut-offs and topic switches mid-utterance
**Observed:** Max started telling a joke. The joke got cut off mid-sentence. Then Gemini-Max picked up with a totally different topic ("nothing to pick up for today").

**Mechanism (compound):**
1. The pipeline has `allow_interruptions=True` on `PipelineParams`. When VAD detects user speech, Max's TTS is cut. This is by design — and is true for Claude-Max too.
2. Because Gemini-Max speaks more often (item 5.1), there are more in-flight Max utterances available to be interrupted.
3. After an interrupt, Gemini received fresh context and generated a *new* response based on that context — apparently calling `get_test_results` or similar — and reported "nothing to pick up." Claude in the same situation would more typically continue the prior thread or stay silent. **Gemini shows lower context-coherence across interrupts** than Claude on this prompt.

### 5.3 Voice breaking
**Observed:** Audio sounded fragmented during some Max utterances.

**Mechanism:** Direct downstream effect of 5.2. Interrupted TTS streams produce fragmented audio.

### 5.4 Many incomplete turns in the timing data
From `/debug/timings`: a meaningful fraction of the 30 turns were marked `incomplete: true` (no `T_mbass_first_send` recorded — meaning Max started a turn but never reached the bridge). With Claude-Max in the 24 April baseline, complete turns were the norm. **This is a quantitative confirmation of the qualitative observation: Gemini-Max gets cut off more.**

---

## 6. Latency comparison

Side-by-side, same TEST meeting on the same week:

| Metric | Claude Haiku 4.5 (24 April) | Gemini 2.5 Flash (28 April) |
|---|---|---|
| Best simple turn | 1,068 ms | **540 ms** |
| Typical simple turn | 1,068–1,157 ms | 1,066–1,198 ms |
| Tool-using turn | 1,708 ms | 1,066–2,074 ms |
| LLM portion (`llm_total_ms`) | not isolated | 524–900 ms |
| Variance | tight | wider |

**Headline:** Gemini's *peak* responsiveness is excellent. Its *consistency* is worse — it varies more turn-to-turn. Whether that trade-off is acceptable depends on the use case.

---

## 7. What would need to change to make Gemini-Max ship-ready

If the team ever wants to revisit Gemini as the production LLM, here is what would have to be tuned (none of these were attempted in this experiment):

1. **Tighten silence rules in the persona.** The "When in doubt, RESPOND" rule needs to be narrowed for Gemini. Possibly: "When in doubt and someone has said your name or addressed you with a question, RESPOND. Otherwise stay silent."
2. **Set `allow_interruptions=False`** on `PipelineParams` — or measure-then-decide. Trade-off: Max can't be cut off mid-sentence by accident, but he also can't be cut off when he's wrong and someone wants to correct him.
3. **Increase VAD `stop_secs`** from 0.4 to something higher (0.6–0.8) to reduce mis-detection of speech endings. Trade-off: slower turn detection.
4. **Constrain response length more aggressively** in the prompt. Gemini has higher length variance.
5. **Test the silence-token discipline.** Persona says "if Max should not speak, return ONLY three dots: ..." — Gemini may not return literal `...` as cleanly as Claude. The `SilenceTextFilter` only catches dot-only TextFrames, so other forms of "I'll just be quiet here" leak through.
6. **Re-test after each change** in a TEST meeting. This is iterative work, probably 2–4 hours of prompt and config tuning.

---

## 8. Configuration to restore Gemini-Max in the future

To redeploy the Gemini configuration:

1. Push or check out `experiment/gemini-llm` branch (already on origin)
2. Verify `GEMINI_API_KEY` is set in Railway env (was left in place after the experiment)
3. Flip Railway's deploy branch from `main` to `experiment/gemini-llm`
4. Wait ~2 minutes for build + deploy
5. Verify `/health` and `/debug/timings`
6. Run a TEST meeting

To revert from Gemini back to Claude: flip Railway deploy branch back to `main`. The Anthropic env var is still set; nothing else needs to change.

---

## 9. Conclusion

The experiment was a clean success on its primary objective: **the persona transfers in concept across LLM vendors when the prompt is well-crafted.** Tool-calling, note-taking, and length discipline survived the swap. Voice and latency profile are comparable.

But the experiment also surfaced **real, measurable, vendor-specific behavioural drift.** The same prompt, same tools, same voice produces a *noticeably different* Max with Gemini at the wheel — chattier, more interruption-prone, less context-coherent across cut-offs. These are not bugs in Gemini; they are the model's default temperament showing through the prompt.

**Production stays on Claude Haiku 4.5** (`main` branch) where Max's behaviour has been tuned and verified across multiple test sessions and is now described in the approval document.

The Gemini experiment is preserved on the `experiment/gemini-llm` branch with this findings document committed alongside, so the work is recoverable in full if the team ever wants to revisit it.

---

## 10. References

- Latency instrumentation merged to main: commit `70cfa23` (24 April 2026)
- This experiment: commit `d469f8e` on `experiment/gemini-llm` (28 April 2026)
- Approval document: `Max_Virtual_Agent_Approval.docx` in `~/Documents/`
- TEST meeting URL: `https://meet.google.com/mmg-mjgn-njd`
- Railway service: `max-brain` in project `sincere-grace`
