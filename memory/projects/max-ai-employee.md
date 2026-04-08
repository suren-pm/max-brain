# Project Max — AI Virtual Employee

**Status:** Active — deployed and working
**Goal:** Max is an AI virtual employee who joins daily Google Meet standups, listens, and responds in real-time. Audio-only mode confirmed and live.

---

## Meeting URLs (CRITICAL)
- **TEST**: https://meet.google.com/mmg-mjgn-njd ← ALWAYS use for testing
- **REAL standup**: https://meet.google.com/agm-fhsi-khp ← NEVER use without explicit permission

---

## ✅ Active Stack (confirmed 01 Apr 2026)
```
Google Meet
  → Meeting BaaS (api.meetingbaas.com/v2) — joins as bot
  → WebSocket wss://max-brain-production.up.railway.app/ws/output/{bot_id}
  → Deepgram STT (nova-2-conversationalai, 16kHz PCM, 2.5s buffer)
  → Claude claude-3-5-haiku-20241022 (Railway brain, 5 tools)
  → Deepgram TTS (aura-arcas-en, linear16 16kHz PCM)
  → WebSocket wss://max-brain-production.up.railway.app/ws/input/{bot_id}
  → Meeting BaaS → Google Meet (Max speaks)
```

**Mode: Audio-only.** No avatar, no HTML overlay, no video. Static profile pic shown in Meet.
No local scripts needed. Everything runs on Railway.

---

## Deployment
| Item | Value |
|------|-------|
| GitHub repo | `suren-pm/max-brain` (branch: main) |
| Auto-deploy | Push to main → Railway deploys automatically |
| Railway project | `sincere-grace` |
| Railway service | `max-brain` (ID: `43b7d1a2-f327-4e6c-8cd3-a6a819922e2a`) |
| Railway project ID | `cd91f9e0-2715-4aaf-b059-4f4784319b59` |
| Railway environment ID | `d5d11d60-1bcf-46a6-8367-d8c8acd5b410` |
| Live URL | https://max-brain-production.up.railway.app |
| Health check | `GET /health` → `{"status":"ok","pending_tasks":N,"done_tasks":N,"test_results":N,"active_streams":N,"briefing_ready":bool}` |

---

## Railway Env Vars (as of 01 Apr 2026)
| Variable | Status | Notes |
|----------|--------|-------|
| ANTHROPIC_API_KEY | ✅ Set | Claude brain |
| DEEPGRAM_API_KEY | ✅ Set | STT + TTS |
| MEETING_BAAS_API_KEY | ✅ Set | Bot joins Meet |
| GOOGLE_MEET_URL | ✅ Set | Points to TEST meeting |
| JIRA_URL | ✅ Set | https://everperform.atlassian.net |
| JIRA_EMAIL | ✅ Set | surendran.kandasamy@everperform.com |
| JIRA_PROJECT_KEY | ✅ Set | ESB |
| MAX_BRAIN_API_KEY | ✅ Set | Protects Railway endpoints |
| RAILWAY_PUBLIC_DOMAIN | ✅ Auto-set | max-brain-production.up.railway.app |
| JIRA_API_TOKEN | ✅ Set | Real-time Jira lookups in standup |

---

## Key Endpoints
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/join` | POST | Trigger Max to join a Google Meet |
| `/ws/output/{bot_id}` | WS | Meeting BaaS → Railway (audio in from meeting) |
| `/ws/input/{bot_id}` | WS | Railway → Meeting BaaS (Max speaking) |
| `/tasks/log` | POST/GET | Log standup tasks / Cowork reads at 10AM |
| `/tasks/result` | POST | Cowork posts test results back |
| `/tasks/results` | GET | Max reads before standup for briefing |
| `/briefing` | POST/GET | Pre-populate standup context |
| `/health` | GET | Server status + active stream count |

---

## Task Loop (Max ↔ Cowork)
1. **Standup:** Max is assigned a ticket → calls `log_task` tool → saved to `/tasks/log` on Railway
2. **10AM:** Cowork reads `/tasks/log` → runs tests on Everperform staging → posts results to `/tasks/result` + #max-ai Slack (C0AQ8M876E5)
3. **Next standup:** Max calls `get_standup_briefing` + `get_test_results` → reports results in standup

---

## Claude Tools in server.py (5 tools)
| Tool | Purpose |
|------|---------|
| `get_jira_ticket` | Fetch a specific Jira ticket |
| `get_testing_tickets` | Get all tickets currently in Testing status |
| `log_task` | Save a task to Railway for Cowork to test |
| `get_test_results` | Read what Cowork tested + results |
| `get_standup_briefing` | Get pre-populated context before standup |

---

## Key Files (what matters now)
| File | Purpose |
|------|---------|
| `max/server.py` | **THE brain** — FastAPI server on Railway |
| `max/persona.py` | Max's system prompt / personality |
| `max/__init__.py` | Package init |
| `Dockerfile` | Railway build (audio-only, no avatar HTML) |
| `railway.toml` | Railway config |
| `requirements-server.txt` | Server dependencies |
| `.gitignore` | Excludes .env, large binaries |
| `.env` | All API keys (local only — NOT in git) |
| `max_face.jpg` | Max's AI face (StyleGAN2) — local only |

---

## Remaining TODOs
1. Verify / set up 10AM Cowork scheduled task (reads `/tasks/log` → tests → posts to `/tasks/result` + #max-ai)

---

## ⚠️ SCRAPPED — Do NOT use
| Thing | Reason |
|-------|--------|
| Recall.ai | Security concerns — scrapped entirely |
| Pipecat / Daily.co | Never part of the stack |
| HeyGen streaming avatar | Deferred indefinitely — audio-only confirmed |
| max_avatar.html overlay | Scrapped — audio-only confirmed |
| SadTalker | Too slow (30s latency for 10s video) |
| MuseTalk | Lip-sync only, abandoned |
| join_standup.py | Used Recall.ai — scrapped |
| start_max.sh | Local launcher — not needed, Max runs on Railway |
| bot.py / bot_free.py | Pipecat-based — not used |

---

## History
- Phase 1: Built MuseTalk lip-sync pipeline — output was static mouth-only, abandoned
- Phase 2: Evaluated SadTalker, HeyGen, D-ID, Colossyan, Synthesia
- Phase 3: Switched to Recall.ai — scrapped due to security concerns
- Phase 4: Rebuilt with Meeting BaaS + WebSocket + Railway — audio-only working
- Phase 5 (01 Apr 2026): Deployed new server.py to Railway via GitHub. All env vars set. Max confirmed working live in test standup.
