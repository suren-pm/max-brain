# Max — AI Automation Test Engineer

Max is a virtual AI employee who joins your daily standups as a real meeting participant. He can hear, speak, take notes, and give his standup update when called on.

## What Max Can Do

- **Join meetings** as a real participant (Google Meet, Zoom, or Teams)
- **Listen** to everyone's updates using real-time speech-to-text
- **Speak** with a natural human voice when addressed
- **Give standup updates** referencing actual Jira tickets and staging status
- **Take meeting notes** automatically — transcript, action items, decisions
- **Answer questions** about testing, bugs, sprint status

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    Meeting Room                      │
│            (Google Meet / Daily.co)                  │
└──────────┬──────────────────────┬───────────────────┘
           │ Audio In             │ Audio Out
           ▼                     ▲
┌──────────────────┐   ┌──────────────────┐
│   Deepgram STT   │   │  Cartesia TTS    │
│  (Max's Ears)    │   │  (Max's Voice)   │
└────────┬─────────┘   └────────▲─────────┘
         │ Text                 │ Text
         ▼                     │
┌──────────────────────────────────────────┐
│          Anthropic Claude                 │
│           (Max's Brain)                   │
│                                          │
│  System Prompt: Max's persona            │
│  Context: Jira sprint + staging health   │
│  Memory: Conversation history            │
└──────────────────────────────────────────┘
```

## Quick Start

### 1. Get API Keys (5 minutes)

You need 4 API keys. All have free tiers:

| Service | What For | Get Key |
|---------|----------|---------|
| **Anthropic** | Claude brain | [console.anthropic.com](https://console.anthropic.com) |
| **Deepgram** | Speech-to-text | [console.deepgram.com](https://console.deepgram.com) |
| **Cartesia** | Text-to-speech | [play.cartesia.ai](https://play.cartesia.ai) |
| **Daily.co** | Meeting room | [dashboard.daily.co](https://dashboard.daily.co) |

Optional (for joining Google Meet directly):

| Service | What For | Get Key |
|---------|----------|---------|
| **Meeting BaaS** | Google Meet connector | [meetingbaas.com](https://www.meetingbaas.com) |

### 2. Install

```bash
cd max-ai-employee
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your API keys
```

### 4. Run Max

**Option A: Daily.co room (simplest — for testing)**
```bash
python run.py --mode daily --with-jira
```
This creates a Daily room and prints a link. Share the link with your team.

**Option B: Join Google Meet directly**
```bash
python run.py --mode meeting_baas --meeting-url "https://meet.google.com/abc-defg-hij" --with-jira
```
Max joins your actual Google Meet as a participant named "Max".

### 5. Interact

Once Max is in the meeting:
- He listens quietly until addressed
- Say **"Hey Max, what's your update?"** and he'll give his standup
- Ask him questions: "Max, what's the status of ESB-1569?"
- He takes notes the entire time and saves them after the meeting

## File Structure

```
max-ai-employee/
├── run.py              # Entry point — launch Max
├── requirements.txt    # Python dependencies
├── .env.example        # API key template
├── README.md           # This file
└── max/
    ├── __init__.py
    ├── bot.py          # Core Pipecat pipeline (STT → LLM → TTS)
    ├── persona.py      # Max's personality and system prompt
    ├── context.py      # Jira + staging data fetcher
    ├── meeting_join.py # Meeting room creation / joining
    └── notes.py        # Meeting note-taking processor
```

## Customizing Max

### Change Max's Voice
Browse voices at [play.cartesia.ai/voices](https://play.cartesia.ai/voices) and update `CARTESIA_VOICE_ID` in `.env`.

### Change Max's Personality
Edit `max/persona.py` — the `SYSTEM_PROMPT` defines how Max behaves, speaks, and what he knows.

### Add Max's Avatar (for video)
For a visual AI avatar in the meeting, integrate with:
- **HeyGen** (streaming avatar) — Pipecat has built-in HeyGen support
- **Tavus** — also supported natively in Pipecat

## Deployment Options

### Run Locally
Just `python run.py` on your machine. Works for testing.

### Run on a Server (recommended for daily standups)
Deploy to any cloud VM:
```bash
# On an AWS EC2 / GCP VM / DigitalOcean Droplet
git clone <repo>
cd max-ai-employee
pip install -r requirements.txt
cp .env.example .env  # Fill in keys

# Run with a scheduler (cron) at standup time
# Example: Run at 9:00 AM AEST every weekday
0 9 * * 1-5 cd /home/ubuntu/max-ai-employee && python run.py --mode meeting_baas --meeting-url "YOUR_MEET_LINK" --with-jira
```

### Run with Docker
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "run.py", "--mode", "daily", "--with-jira"]
```

## Roadmap

- [x] Core voice pipeline (STT → Claude → TTS)
- [x] Daily.co meeting room support
- [x] Meeting BaaS integration (Google Meet / Zoom / Teams)
- [x] Jira sprint context fetching
- [x] Real-time meeting note-taking
- [ ] HeyGen avatar integration (Max gets a face)
- [ ] Scheduled auto-join (Max joins standup automatically every morning)
- [ ] Post-meeting Slack summary (Max posts notes to #standup channel)
- [ ] Jira ticket creation from meeting action items
- [ ] Multi-meeting support (Max attends multiple standups)

## Tech Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Framework | [Pipecat](https://github.com/pipecat-ai/pipecat) | Voice agent orchestration |
| Brain | [Anthropic Claude](https://anthropic.com) | Reasoning & conversation |
| Ears | [Deepgram](https://deepgram.com) | Speech-to-text |
| Voice | [Cartesia](https://cartesia.ai) | Text-to-speech |
| Meeting | [Daily.co](https://daily.co) / [Meeting BaaS](https://meetingbaas.com) | Video call transport |

## Cost Estimate (per standup)

Assuming a 15-minute standup where Max speaks for ~2 minutes:

| Service | Usage | Cost |
|---------|-------|------|
| Claude Sonnet | ~5K tokens | ~$0.02 |
| Deepgram | ~15 min audio | ~$0.04 |
| Cartesia | ~500 chars output | ~$0.01 |
| Daily.co | 1 participant-minute | Free tier |
| **Total** | | **~$0.07/standup** |

That's about **$1.50/month** for daily standups. Cheaper than a coffee.
