"""
Max's Railway Brain Server
==========================
Stack: Meeting BaaS (audio I/O) → Deepgram STT → Claude (with tools) → Deepgram TTS

Audio flow:
  Google Meet audio  →  Meeting BaaS  →  wss://.../ws/output  →  Deepgram STT  →  Claude
  Claude response    →  Deepgram TTS  →  wss://.../ws/input   →  Meeting BaaS  →  Google Meet

Endpoints:
  POST /join                → trigger Max to join a meeting
  WS   /ws/output/{bot_id} → receive meeting audio (PCM 16kHz)
  WS   /ws/input/{bot_id}  → serve Max's voice audio (PCM 16kHz)
  POST /tasks/log           → log task assigned in standup
  GET  /tasks/log           → Cowork reads this at 10AM
  POST /tasks/result        → Cowork posts test results
  GET  /tasks/results       → Max reads before standup (all results + pending)
  GET  /health
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from typing import Optional

import anthropic as anthropic_sdk
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

app = FastAPI(title="Max Brain Server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Constants ──────────────────────────────────────────────────────────────────
MEETING_BAAS_API     = "https://api.meetingbaas.com/v2"
DEEPGRAM_TTS_MODEL   = "aura-arcas-en"
DEEPGRAM_STT_MODEL   = "nova-2-conversationalai"
SAMPLE_RATE          = 16000                         # 16 kHz PCM
BUFFER_SECS          = 2.5                           # STT batch window
CHUNK_SIZE           = int(SAMPLE_RATE * BUFFER_SECS * 2)  # bytes (16-bit)

TRIGGER_RE = re.compile(
    r"\bmax\b|your (update|turn|standup)|go ahead|"
    r"what (did|do|have) you|did you (test|check|verify)|"
    r"any (blockers?|issues?|updates?)|hey max",
    re.IGNORECASE,
)

# ── In-memory state (persists while Railway process runs) ───────────────────────
audio_input_queues: dict[str, asyncio.Queue] = {}
audio_buffers:      dict[str, bytes]         = {}
conversation:       list[dict]               = []
pending_tasks:      list[dict]               = []
test_results:       list[dict]               = []
briefing_cache:     str                      = ""   # latest Slack briefing stored by Cowork

# ── Anthropic client ────────────────────────────────────────────────────────────
_anthropic: Optional[anthropic_sdk.AsyncAnthropic] = None

def get_anthropic() -> anthropic_sdk.AsyncAnthropic:
    global _anthropic
    if _anthropic is None:
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError("ANTHROPIC_API_KEY not set")
        _anthropic = anthropic_sdk.AsyncAnthropic(api_key=key)
    return _anthropic


# ── Deepgram helpers ────────────────────────────────────────────────────────────

async def pcm_to_text(pcm_bytes: bytes) -> str:
    """Transcribe raw 16kHz linear16 PCM via Deepgram batch STT."""
    key = os.getenv("DEEPGRAM_API_KEY")
    if not key:
        return ""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.deepgram.com/v1/listen"
                f"?model={DEEPGRAM_STT_MODEL}&encoding=linear16"
                f"&sample_rate={SAMPLE_RATE}&language=en&smart_format=true",
                headers={
                    "Authorization": f"Token {key}",
                    "Content-Type": "audio/raw",
                },
                content=pcm_bytes,
                timeout=15,
            )
        if resp.status_code == 200:
            channels = resp.json().get("results", {}).get("channels", [{}])
            alts = channels[0].get("alternatives", [{}])
            return alts[0].get("transcript", "").strip()
        logger.warning(f"STT {resp.status_code}: {resp.text[:80]}")
    except Exception as e:
        logger.error(f"STT error: {e}")
    return ""


async def text_to_pcm(text: str) -> bytes:
    """Generate 16kHz linear16 PCM from text via Deepgram TTS."""
    key = os.getenv("DEEPGRAM_API_KEY")
    if not key:
        return b""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.deepgram.com/v1/speak"
                f"?model={DEEPGRAM_TTS_MODEL}&encoding=linear16&sample_rate={SAMPLE_RATE}",
                headers={
                    "Authorization": f"Token {key}",
                    "Content-Type": "application/json",
                },
                json={"text": text},
                timeout=20,
            )
        if resp.status_code == 200:
            return resp.content  # raw PCM bytes
        logger.warning(f"TTS {resp.status_code}: {resp.text[:80]}")
    except Exception as e:
        logger.error(f"TTS error: {e}")
    return b""


# ── Jira helpers ────────────────────────────────────────────────────────────────

def _jira_auth() -> Optional[str]:
    email = os.getenv("JIRA_EMAIL")
    token = os.getenv("JIRA_API_TOKEN")
    if not (email and token):
        return None
    return base64.b64encode(f"{email}:{token}".encode()).decode()


async def jira_get_ticket(ticket_id: str) -> dict:
    auth = _jira_auth()
    if not auth:
        return {"error": "Jira not configured (JIRA_API_TOKEN missing)"}
    base = os.getenv("JIRA_URL", "https://everperform.atlassian.net")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{base}/rest/api/3/issue/{ticket_id}"
                f"?fields=summary,status,assignee,priority,description",
                headers={"Authorization": f"Basic {auth}", "Accept": "application/json"},
                timeout=10,
            )
        if resp.status_code == 200:
            d = resp.json()
            f = d.get("fields", {})
            return {
                "id":       d.get("key"),
                "summary":  f.get("summary"),
                "status":   f.get("status", {}).get("name"),
                "assignee": (f.get("assignee") or {}).get("displayName", "unassigned"),
                "priority": (f.get("priority") or {}).get("name"),
            }
        return {"error": f"Jira {resp.status_code}"}
    except Exception as e:
        return {"error": str(e)}


async def jira_testing_tickets() -> list[dict]:
    auth = _jira_auth()
    if not auth:
        return []
    base    = os.getenv("JIRA_URL", "https://everperform.atlassian.net")
    project = os.getenv("JIRA_PROJECT_KEY", "ESB")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{base}/rest/api/3/search"
                f'?jql=project={project}+AND+status="Testing"+ORDER+BY+priority+DESC'
                f"&fields=summary,status,assignee&maxResults=15",
                headers={"Authorization": f"Basic {auth}", "Accept": "application/json"},
                timeout=10,
            )
        if resp.status_code == 200:
            return [
                {
                    "id":       i["key"],
                    "summary":  i["fields"]["summary"],
                    "status":   i["fields"]["status"]["name"],
                    "assignee": (i["fields"].get("assignee") or {}).get("displayName", "unassigned"),
                }
                for i in resp.json().get("issues", [])
            ]
    except Exception as e:
        logger.error(f"Jira query error: {e}")
    return []


# ── Claude tools ────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_jira_ticket",
        "description": "Fetch details of a specific Jira ticket by ID (e.g. ESB-1275). Use when someone asks about a ticket in standup.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string", "description": "Jira ticket ID, e.g. ESB-1275"}
            },
            "required": ["ticket_id"],
        },
    },
    {
        "name": "get_testing_tickets",
        "description": "Get all Jira tickets currently in Testing status. Use when giving standup updates about what's in testing.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "log_task",
        "description": "Log a task assigned to you in standup so Cowork (the testing agent) can pick it up at 10AM and run the tests.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id":   {"type": "string", "description": "Jira ticket ID"},
                "description": {"type": "string", "description": "What needs to be tested"},
            },
            "required": ["ticket_id"],
        },
    },
    {
        "name": "get_test_results",
        "description": "Get test results posted by Cowork. Use at the start of standup to report on what was tested yesterday.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_standup_briefing",
        "description": "Get the latest standup briefing — completed test results and pending tasks. Use when asked for your standup update.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


async def run_tool(name: str, inputs: dict) -> str:
    if name == "get_jira_ticket":
        result = await jira_get_ticket(inputs.get("ticket_id", ""))
        return json.dumps(result)

    elif name == "get_testing_tickets":
        tickets = await jira_testing_tickets()
        return json.dumps(tickets)

    elif name == "log_task":
        task = {
            "ticket_id":   inputs.get("ticket_id"),
            "description": inputs.get("description", ""),
            "logged_at":   time.strftime("%Y-%m-%d %H:%M IST"),
            "status":      "pending",
        }
        pending_tasks.append(task)
        logger.info(f"📋 Task logged: {task['ticket_id']}")
        return json.dumps({"ok": True, "task": task})

    elif name == "get_test_results":
        return json.dumps({
            "results": test_results[-10:],
            "pending": [t for t in pending_tasks if t.get("status") == "pending"],
        })

    elif name == "get_standup_briefing":
        completed = [r for r in test_results if r]
        pending   = [t for t in pending_tasks if t.get("status") == "pending"]
        briefing  = briefing_cache or "No briefing available yet."
        return json.dumps({
            "briefing":         briefing,
            "completed_tests":  completed[-5:],
            "pending_tasks":    pending,
        })

    return json.dumps({"error": f"Unknown tool: {name}"})


async def claude_respond(speaker: str, transcript: str) -> str:
    """Run Claude with tools, return final text response."""
    from max.persona import SYSTEM_PROMPT

    conversation.append({
        "role":    "user",
        "content": f'{speaker} says: "{transcript}"',
    })
    history = conversation[-20:]  # keep context window manageable

    client = get_anthropic()
    msg = await client.messages.create(
        model="claude-3-5-haiku-20241022",
        max_tokens=250,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        messages=history,
    )

    # Agentic loop — handle tool calls
    while msg.stop_reason == "tool_use":
        tool_results = []
        for block in msg.content:
            if block.type == "tool_use":
                logger.info(f"🔧 Tool call: {block.name}({block.input})")
                result = await run_tool(block.name, block.input)
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     result,
                })

        history.append({"role": "assistant", "content": msg.content})
        history.append({"role": "user",      "content": tool_results})

        msg = await client.messages.create(
            model="claude-3-5-haiku-20241022",
            max_tokens=250,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=history,
        )

    # Extract text
    response = " ".join(
        block.text for block in msg.content if hasattr(block, "text")
    ).strip()

    if response:
        conversation.append({"role": "assistant", "content": response})

    return response


# ── Audio processing ────────────────────────────────────────────────────────────

async def process_audio_chunk(bot_id: str, pcm: bytes) -> None:
    """STT → trigger check → Claude → TTS → queue audio for Meeting BaaS."""
    transcript = await pcm_to_text(pcm)
    if not transcript:
        return

    logger.info(f"🎤 [{bot_id}] {transcript[:100]}")

    if not TRIGGER_RE.search(transcript):
        return

    logger.info(f"💬 Max triggered: {transcript[:80]}")
    response = await claude_respond("Team", transcript)
    if not response:
        return

    logger.info(f"🤖 Max: {response[:100]}")
    audio_pcm = await text_to_pcm(response)
    if audio_pcm:
        queue = audio_input_queues.get(bot_id)
        if queue:
            await queue.put(audio_pcm)


# ── WebSocket: Meeting BaaS audio output (meeting → Railway) ────────────────────

@app.websocket("/ws/output/{bot_id}")
async def ws_output(websocket: WebSocket, bot_id: str):
    """Meeting BaaS sends raw 16kHz PCM audio here (what participants say).
    Handles both binary frames (raw PCM) and text frames (JSON with base64 audio)."""
    await websocket.accept()
    audio_buffers[bot_id] = b""
    logger.info(f"🎙️  Output stream connected — bot: {bot_id}")

    try:
        while True:
            msg = await websocket.receive()

            # Binary frame → raw PCM
            if "bytes" in msg and msg["bytes"]:
                audio_buffers[bot_id] += msg["bytes"]

            # Text frame → JSON envelope with base64 audio (Meeting BaaS v2 format)
            elif "text" in msg and msg["text"]:
                try:
                    payload = json.loads(msg["text"])
                    raw = payload.get("data") or payload.get("audio") or ""
                    if raw:
                        audio_buffers[bot_id] += base64.b64decode(raw)
                except Exception as e:
                    logger.warning(f"WS text parse error: {e}")

            if len(audio_buffers[bot_id]) >= CHUNK_SIZE:
                chunk = audio_buffers[bot_id]
                audio_buffers[bot_id] = b""
                asyncio.create_task(process_audio_chunk(bot_id, chunk))

    except WebSocketDisconnect:
        logger.info(f"🔌 Output stream disconnected — bot: {bot_id}")
        audio_buffers.pop(bot_id, None)
    except Exception as e:
        logger.error(f"❌ ws_output error: {e}")
        audio_buffers.pop(bot_id, None)


# ── WebSocket: Meeting BaaS audio input (Railway → meeting) ────────────────────

@app.websocket("/ws/input/{bot_id}")
async def ws_input(websocket: WebSocket, bot_id: str):
    """Meeting BaaS pulls raw PCM audio from here to play in the meeting (Max speaking)."""
    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue()
    audio_input_queues[bot_id] = queue
    logger.info(f"🔊 Input stream connected — bot: {bot_id}")

    # Greet the team as soon as we're in the meeting
    async def _greet():
        await asyncio.sleep(2.0)          # let the connection settle first
        pcm = await text_to_pcm("Hey team! Max here. Ready for standup.")
        if pcm:
            await queue.put(pcm)
            logger.info("👋 Greeting queued")

    asyncio.create_task(_greet())

    try:
        while True:
            try:
                audio_pcm = await asyncio.wait_for(queue.get(), timeout=30.0)
                await websocket.send_bytes(audio_pcm)
                logger.info(f"🔊 Sent {len(audio_pcm):,} bytes to meeting")
            except asyncio.TimeoutError:
                # Send a silent keep-alive frame so Meeting BaaS doesn't drop us
                silence = b"\x00\x00" * SAMPLE_RATE  # 1s of silence
                await websocket.send_bytes(silence)
    except WebSocketDisconnect:
        logger.info(f"🔌 Input stream disconnected — bot: {bot_id}")
        audio_input_queues.pop(bot_id, None)
    except Exception as e:
        logger.error(f"❌ ws_input error: {e}")
        audio_input_queues.pop(bot_id, None)


# ── Join endpoint ───────────────────────────────────────────────────────────────

@app.post("/join")
async def join_meeting(request: Request):
    """Trigger Max to join a Google Meet via Meeting BaaS."""
    body         = await request.json()
    meeting_url  = body.get("meeting_url") or os.getenv("GOOGLE_MEET_URL", "")
    bot_name     = body.get("bot_name", "Max")

    if not meeting_url:
        return {"error": "meeting_url required (or set GOOGLE_MEET_URL env var)"}

    # Build public WebSocket URLs from Railway domain
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    if not domain:
        return {"error": "RAILWAY_PUBLIC_DOMAIN env var not set on Railway"}

    # NOTE: streaming_input  = URL where Meeting BaaS SENDS meeting audio to us (bot listens)
    #       streaming_output = URL where Meeting BaaS READS bot audio from us (bot speaks)
    # DO NOT set enter_message — Max speaks only via voice, never via chat
    payload = {
        "bot_name":                        bot_name,
        "meeting_url":                     meeting_url,
        "recording_mode":                  "audio_only",
        "streaming_input":                 f"wss://{domain}/ws/output/max",
        "streaming_output":                f"wss://{domain}/ws/input/max",
        "streaming_audio_frequency":       "16khz",
        "webhook_url":                     f"https://{domain}/webhook",
        "extra":                           {},
        "transcription_custom_parameters": {},
    }

    api_key = os.getenv("MEETING_BAAS_API_KEY", "")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{MEETING_BAAS_API}/bots",
            headers={
                "x-meeting-baas-api-key": api_key,
                "Content-Type":           "application/json",
            },
            json=payload,
            timeout=30,
        )

    if resp.status_code in (200, 201):
        result = resp.json()
        data   = result.get("data") or result
        bot_id = data.get("bot_id") or data.get("id") or result.get("bot_id") or "unknown"
        logger.info(f"✅ Max joining {meeting_url} — bot_id: {bot_id}")
        return {"ok": True, "bot_id": bot_id, "meeting_url": meeting_url}

    logger.error(f"❌ Meeting BaaS {resp.status_code}: {resp.text[:200]}")
    return {"error": f"Meeting BaaS {resp.status_code}", "detail": resp.text[:200]}


# ── Task management endpoints ───────────────────────────────────────────────────

@app.post("/tasks/log")
async def post_task(request: Request):
    """Log a testing task. Called by Max during standup (via tool) or manually."""
    task = await request.json()
    task.setdefault("logged_at", time.strftime("%Y-%m-%d %H:%M IST"))
    task.setdefault("status", "pending")
    pending_tasks.append(task)
    logger.info(f"📋 Task logged: {task.get('ticket_id')} — {task.get('description', '')}")
    return {"ok": True, "task": task, "total_pending": len(pending_tasks)}


@app.get("/tasks/log")
async def get_tasks():
    """Cowork reads this at 10AM to pick up testing tasks."""
    return {
        "tasks":         pending_tasks,
        "pending_count": len([t for t in pending_tasks if t.get("status") == "pending"]),
        "as_of":         time.strftime("%Y-%m-%d %H:%M IST"),
    }


@app.post("/tasks/result")
async def post_result(request: Request):
    """Cowork posts test results here after running tests."""
    result = await request.json()
    result.setdefault("posted_at", time.strftime("%Y-%m-%d %H:%M IST"))
    test_results.append(result)

    # Mark matching pending task as done
    for t in pending_tasks:
        if t.get("ticket_id") == result.get("ticket_id"):
            t["status"] = "done"
            break

    logger.info(
        f"✅ Result: {result.get('ticket_id')} — "
        f"{'PASS' if result.get('passed') else 'FAIL'}"
    )
    return {"ok": True, "result": result}


@app.get("/tasks/results")
async def get_results():
    """Max reads this before standup to report on completed tests."""
    return {
        "results":      test_results,
        "pending":      [t for t in pending_tasks if t.get("status") == "pending"],
        "done":         [t for t in pending_tasks if t.get("status") == "done"],
        "as_of":        time.strftime("%Y-%m-%d %H:%M IST"),
    }


@app.post("/briefing")
async def post_briefing(request: Request):
    """Cowork posts the latest standup briefing here (summary of Slack updates)."""
    global briefing_cache
    body = await request.json()
    briefing_cache = body.get("briefing", "")
    logger.info("📰 Briefing updated by Cowork")
    return {"ok": True}


@app.get("/briefing")
async def get_briefing():
    """Max reads this for standup context."""
    return {"briefing": briefing_cache, "as_of": time.strftime("%Y-%m-%d %H:%M IST")}


# ── Meeting BaaS webhook ────────────────────────────────────────────────────────

@app.post("/webhook")
async def meeting_baas_webhook(request: Request):
    """Receives Meeting BaaS lifecycle events (bot.joining, transcript.update, etc).
    Max listens to chat messages here but NEVER replies via chat — voice only."""
    try:
        event = await request.json()
    except Exception:
        return {"ok": True}

    event_type = event.get("event") or event.get("type", "unknown")
    logger.info(f"📡 Webhook: {event_type}")

    # Log chat messages from the meeting so Max can use them as context
    if event_type in ("transcript.update", "chat.message"):
        speaker = event.get("speaker") or event.get("sender", "Someone")
        text     = event.get("transcript") or event.get("text", "")
        if text:
            logger.info(f"💬 [chat/transcript] {speaker}: {text[:120]}")
            # Inject into conversation context so Claude can reference it
            conversation.append({
                "role":    "user",
                "content": f'[Meeting chat from {speaker}]: "{text}"',
            })

    return {"ok": True}


# ── Health ──────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status":          "ok",
        "pending_tasks":   len([t for t in pending_tasks if t.get("status") == "pending"]),
        "done_tasks":      len([t for t in pending_tasks if t.get("status") == "done"]),
        "test_results":    len(test_results),
        "active_streams":  len(audio_input_queues),
        "briefing_ready":  bool(briefing_cache),
    }
