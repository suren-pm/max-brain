# Design — Max Self-Hosted Bot (v1)

**Author:** Claude + Suren
**Date:** 2026-04-28
**Status:** Approved (verbal) — ready for implementation
**Phase:** 1 of 3 (self-host the bot layer)
**Repo:** new — `suren-pm/max-bot` (forked from `Meeting-BaaS/meet-teams-bot`)
**Railway project:** new — `max-self-hosted`

---

## 1. Why

After the 2026-04-28 demo (~50% success, see `project_max_demo_outcome_apr28.md` in auto-memory), Suren made the strategic decision to remove MBaaS from Max's architecture entirely. The driver is not cost — at current scale the math is roughly break-even — but **iteration freedom**. MBaaS has no free sandbox tier; every test costs tokens. That constraint blocked the testing volume needed to find and fix Max's quality bugs before the demo. Self-hosting the bot eliminates per-minute MBaaS charges so testing becomes effectively free, accelerating the path to a demo that's perfect rather than 50% perfect.

## 2. v1 scope (locked)

- **Google Meet only.** Microsoft Teams support deferred.
- **Anonymous guest join.** Phase 2 upgrade to real Google Workspace account (fixes anti-bot detection AND the participant-list "M" letter sidebar avatar).
- **Single bot at a time.** Multi-concurrent-meetings deferred.
- **Same persona, voice, avatar.** V1.4 identity (Max as Maxine, Aura Asteria, widescreen avatar) is preserved end-to-end.

## 3. Architecture

```
Google Meet (browser)
    ↕ audio (WebRTC, normal Meet flow)
┌─────────────────────────────────────────────┐
│  max-bot Railway service (NEW)              │
│  - Forked meet-teams-bot (TypeScript)       │
│  - Playwright + Chromium in Docker          │
│  - HTTP API: POST /join, POST /leave/{id},  │
│              GET /health                    │
│  - WebSocket: /ws/{bot_id}                  │
│      out: meeting audio → max-brain         │
│      in:  TTS audio ← max-brain             │
│  - Audio routing: PulseAudio + ffmpeg       │
│      capture: Chrome audio out → PCM        │
│      injection: PCM → virtual mic → Chrome  │
└─────────────────────────────────────────────┘
    ↕ WebSocket (raw PCM 16kHz mono — same protocol MBaaS uses today)
┌─────────────────────────────────────────────┐
│  max-brain Railway service (existing, frozen) │
│  - Pipecat pipeline unchanged                │
│  - VAD → STT → LLM → TTS                    │
│  - /join handler edited to call max-bot     │
│    instead of api.meetingbaas.com           │
└─────────────────────────────────────────────┘
```

**Key design choice:** the WebSocket protocol between max-bot and max-brain is **byte-identical** to MBaaS's protocol today (raw PCM 16kHz mono over WebSocket). This makes max-brain a drop-in replacement target — its WebSocket bridge code is unchanged. The only edit on max-brain is the URL it posts to in `/join`.

## 4. Components

### 4.1 max-bot service (NEW)

**HTTP API**
- `POST /join` — body `{meeting_url, bot_name, bot_image_url, callback_url}`. Spawns a Chromium instance, joins the meeting, returns `{bot_id}`. Mirrors MBaaS's `POST /v2/bots` minimally.
- `POST /leave/{bot_id}` — graceful disconnect.
- `GET /health` — uptime, active bots count, system status.

**Playwright orchestrator**
- Drives Chromium to `meet.google.com`
- Fills in display name from `bot_name`
- Clicks "Ask to join" / "Join now"
- Detects when admitted (DOM signal in Meet's UI)
- Monitors for meeting-ended state, triggers cleanup

**Audio capture (out from meeting)**
- PulseAudio sink inside the container captures Chrome's audio output
- ffmpeg resamples to 16kHz mono PCM
- 100ms chunks streamed to max-brain over WebSocket

**Audio injection (in to meeting)**
- WebSocket receives raw PCM from max-brain
- Writes to a PulseAudio source
- Source is wired as Chrome's microphone input via `getUserMedia` constraints
- Chrome's WebRTC sends it out to Meet, other participants hear it as the bot's voice

### 4.2 max-brain service (existing, minimal change)

**Frozen state:** code is preserved as commit `2082cbd` on `main` in `suren-pm/max-brain`. We do NOT modify it during Phase 1 except to point it at max-bot in Milestone E.

**Changes at Milestone E (after max-bot is verified working):**
1. `max/server.py` `/join` handler — replace MBaaS endpoint URL with `${BOT_SERVICE_URL}/join`
2. Strip MBaaS-specific request fields (`streaming_enabled`, `streaming_config`, `recording_mode`, `transcription_enabled`, `bot_image`, timeouts) — replace with max-bot's request body
3. Add `BOT_SERVICE_URL` env var to Railway, optionally retain `MEETING_BAAS_API_KEY` as inactive fallback

That's it. Roughly 30 lines of code change in max-brain.

## 5. Data flow (one full turn)

1. Person speaks in Google Meet
2. Chromium receives audio → Chrome's audio-out
3. PulseAudio sink captures it
4. ffmpeg resamples to 16kHz mono PCM
5. WebSocket sends 100ms chunks to max-brain's `/ws/{bot_id}`
6. max-brain's bridge (existing code, unchanged) converts raw PCM → protobuf → Pipecat
7. Pipecat pipeline (unchanged): Silero VAD → Deepgram STT → Claude Haiku 4.5 → silence filter → Deepgram TTS
8. TTS audio (raw PCM) flows back through bridge → WebSocket → max-bot
9. max-bot writes PCM to PulseAudio source
10. Chrome's `getUserMedia` reads from that source as the mic input
11. WebRTC encodes and sends to Meet
12. Other participants hear Max's voice

## 6. Build milestones (within Phase 1)

| # | Milestone | Acceptance criteria | Estimate |
|---|---|---|---|
| A | Fork `Meeting-BaaS/meet-teams-bot` → `suren-pm/max-bot`. Create new Railway project `max-self-hosted`. Deploy. | `/health` returns 200 | 1–2 days |
| B | Playwright joins a real Google Meet URL with `bot_name=Max`. | Screenshot confirms bot in waiting room of TEST meeting | 3–5 days |
| C | Audio capture out — meeting audio reaches a test WebSocket as 16kHz mono PCM | Captured stream playable as audio | 3–5 days |
| D | Audio injection in — push pre-recorded PCM to bot, Meet participants hear it. Hardest milestone (PulseAudio + Chrome routing) | A pre-recorded clip plays into the meeting from the bot side | 5–7 days |
| E | Integrate with max-brain — change `/join` target, full end-to-end conversation works | Suren joins TEST meeting, triggers via curl, has a real conversation with Max | 2–3 days |
| F | Hardening — auto-restart on crash, cleanup on meeting end, basic anti-bot resilience | Stable for 30+ min uninterrupted session | 3–5 days |

**Total v1: approximately 3–4 weeks of focused work.**

## 7. Risks and mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| Google Meet anti-bot detection flags headless Chromium | High | Use Playwright stealth mode, full Chrome (not headless-Chromium), realistic User-Agent. Phase 2 Workspace account eliminates this entirely. |
| Docker audio routing complexity (PulseAudio is finicky in containers) | High | Heavy testing in Milestones C/D. Documented well-known issue. Fallback: deploy to a dedicated Ubuntu VPS instead of Railway if Docker proves intractable. |
| WebRTC echo cancellation dampens Max's injected voice | Medium | Tune mic input gain. Test extensively at Milestone D. Worst case: disable WebRTC noise suppression via Chrome flags. |
| Google Meet shows "this looks like a bot" warning during demo | Medium | Phase 2 Workspace account fixes this. For v1: present the bot as honestly a bot in persona, not try to hide it. |
| Resource cost on Railway (Chrome + PulseAudio is RAM/CPU heavy) | Low | Right-size service, monitor. Expect ~$15–25/month for the bot service alone. |
| Meet UI changes break Playwright selectors | Ongoing | Use semantic selectors not CSS classes. Monitor for changes. Address as it arises — open-source community fixes are frequent. |

## 8. Explicitly out of scope for v1

- Microsoft Teams support
- Multiple concurrent meetings (bot in two Meets simultaneously)
- Real Google Workspace account auth (Phase 2 — solves anti-bot AND sidebar avatar)
- Bot avatar control beyond what current MBaaS gives us (Phase 2)
- Meeting recording / transcript dump artifacts (max-brain processes audio live, doesn't need recording)
- Stealth/anti-detection beyond Playwright defaults
- Auto-scaling, load balancing (single bot at a time, single Railway instance)

## 9. Operational separation

- **`sincere-grace` Railway project** — preserved. `max-brain` paused (active deployment removed). Code intact on GitHub, env vars intact in Railway. One-click revival if anything goes wrong with self-hosted work.
- **`max-self-hosted` Railway project** — NEW. Contains only `max-bot` initially. max-brain may be migrated here later if tighter coupling becomes useful.
- **Cross-project networking:** max-bot connects to max-brain via the public Railway URL (`https://max-brain-production.up.railway.app`) over WSS. Standard public-internet WebSocket, encrypted, fine for our latency needs.

## 10. Phase 2 (post-demo, not in this spec)

Captured here so future-Claude doesn't lose context:

- Real Google Workspace account (`max@everperform.com`) → OAuth → MBaaS-style `credential_id` field equivalent in max-bot — fixes anti-bot detection AND participant-list "M" letter
- Sliding context window on max-brain (fixes the 4–5s delay and 15-min silence-loop)
- Microsoft Teams support
- Multi-meeting concurrency

Phase 2 begins only after Phase 1 ships and the next demo is rehearsed to perfection.

---

## Appendix A — meet-teams-bot reference notes

- **Language:** TypeScript (90%), Shell (9%)
- **Runtime:** Node.js, Playwright, Chromium in Docker
- **License:** Elastic License 2.0 — permits self-hosting / forking, not redistribution as a competing service. Our use (forking for internal Max only) is fully within license terms.
- **Maintainer:** Meeting-BaaS team (same team as the managed service). Active development, recent commits.
- **Existing capability:** records meeting audio to local disk. Does NOT do bidirectional streaming out of the box — that's our additional work.
- **Known Docker limitation:** Teams video streams don't always work in Docker per the README. Mitigation: we're Meet-only for v1.
