"""
Max's Railway Brain Server
==========================
Stack: Meeting BaaS (audio I/O) → Deepgram STT → Claude (with tools) → Deepgram TTS

Audio flow (BIDIRECTIONAL — single WebSocket endpoint):
  Google Meet audio → Meeting BaaS → wss://.../ws/max → Deepgram STT → Claude
  Claude response   → Deepgram TTS → wss://.../ws/max → Meeting BaaS → Google Meet

Meeting BaaS v2 API: POST https://api.meetingbaas.com/v2/bots
  streaming_enabled: true  (defaults false — MUST set explicitly!)
  streaming_config: { input_url, output_url, audio_frequency: 24000 }
  audio_frequency supports: 24000, 32000, 48000 Hz (NOT 16000!)
Both input_url AND output_url point to the SAME bidirectional WebSocket /ws/{bot_id}.

Endpoints:
  POST /join           → trigger Max to join a meeting
  WS   /ws/{bot_id}   → bidirectional audio with Meeting BaaS (receive + send)
  POST /tasks/log      → log task assigned in standup
  GET  /tasks/log      → Cowork reads this at 10AM
  POST /tasks/result   → Cowork posts test results
  GET  /tasks/results  → Max reads before standup
  GET  /health
  GET  /debug
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
MEETING_BAAS_API     = "https://api.meetingbaas.com/v2"  # v2 API — nested streaming_config
DEEPGRAM_TTS_MODEL   = "aura-arcas-en"
DEEPGRAM_STT_MODEL   = "nova-2-conversationalai"
SAMPLE_RATE          = 24000                         # 24 kHz — v2 API supports 24000/32000/48000 only (NOT 16000)
BUFFER_SECS          = 2.0                           # STT batch window — 2s balances speed vs transcript quality
CHUNK_SIZE           = int(SAMPLE_RATE * BUFFER_SECS * 2)  # bytes (16-bit) = 96000 bytes at 24kHz
SILENCE_FRAME        = b"\x00\x00" * int(SAMPLE_RATE * 0.1)  # 100ms silence keep-alive = 4800 bytes

# ── In-memory state (persists while Railway process runs) ───────────────────────
audio_input_queues: dict[str, asyncio.Queue] = {}
audio_buffers:      dict[str, bytes]         = {}
conversation:       list[dict]               = []
speaking_until:     float                    = 0.0  # timestamp when Max finishes speaking — suppress echo
greeting_sent:      bool                     = False  # prevent double greeting
pending_tasks:      list[dict]               = []
test_results:       list[dict]               = []
recent_transcripts: list[dict]               = []   # last 20 STT results for debugging
briefing_cache:     str                      = ""   # latest Slack briefing stored by Cowork

# ── Transcript accumulator ─────────────────────────────────────────────────────
# Collects STT fragments and waits for a pause before sending to Claude.
# Fixes sentence-splitting: "Can you give me" + "your updates Max" → one message.
ACCUMULATOR_PAUSE   = 3.0   # seconds of silence before flushing to Claude (2.0 was too short — users pause mid-sentence)
transcript_fragments: list[str] = []
_flush_task: Optional[asyncio.Task] = None

# ── Audio pipeline counters (for /debug diagnosis) ──────────────────────────────
audio_log: list[str] = []   # last 50 key events with timestamps

def alog(msg: str) -> None:
    """Append a timestamped event to audio_log (capped at 50 entries)."""
    audio_log.append(f"{time.strftime('%H:%M:%S')} {msg}")
    if len(audio_log) > 50:
        audio_log.pop(0)

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
    """Transcribe raw 24kHz linear16 PCM via Deepgram batch STT."""
    key = os.getenv("DEEPGRAM_API_KEY")
    if not key:
        return ""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.deepgram.com/v1/listen"
                f"?model={DEEPGRAM_STT_MODEL}&encoding=linear16"
                f"&sample_rate={SAMPLE_RATE}&language=en&smart_format=true"
                f"&keywords=Max:15&keywords=Jira:10&keywords=EverPerform:5&keywords=ESB:10"
                f"&keywords=ticket:5&keywords=standup:5&keywords=testing:5&keywords=blocker:5"
                f"&keywords=Mike:5&keywords=Mack:5",
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
    """Generate 24kHz linear16 PCM from text via Deepgram TTS."""
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
            alog(f"TTS OK {len(resp.content):,} bytes (text={text[:30]!r})")
            return resp.content  # raw PCM bytes
        alog(f"TTS FAIL {resp.status_code}: {resp.text[:60]}")
        logger.warning(f"TTS {resp.status_code}: {resp.text[:80]}")
    except Exception as e:
        alog(f"TTS EXC: {e}")
        logger.error(f"TTS error: {e}")
    return b""


# ── Jira helpers ────────────────────────────────────────────────────────────────

def _jira_auth() -> Optional[str]:
    email = os.getenv("JIRA_EMAIL")
    token = os.getenv("JIRA_API_TOKEN")
    if not (email and token):
        return None
    return base64.b64encode(f"{email}:{token}".encode()).decode()


def _normalize_ticket_id(raw: str) -> str:
    """Normalize a ticket ID from STT-mangled input.
    Examples: '399' → 'ESB-399', 'ESB-1399' → 'ESB-1399', '1399' → 'ESB-1399'
    """
    raw = raw.strip().upper()
    # Already has project prefix
    if re.match(r"[A-Z]+-\d+", raw):
        return raw
    # Just a number — add ESB- prefix
    digits = re.sub(r"[^0-9]", "", raw)
    if digits:
        project = os.getenv("JIRA_PROJECT_KEY", "ESB")
        return f"{project}-{digits}"
    return raw


async def jira_get_ticket(ticket_id: str) -> dict:
    auth = _jira_auth()
    if not auth:
        return {"error": "Jira not configured (JIRA_API_TOKEN missing)"}
    base = os.getenv("JIRA_URL", "https://everperform.atlassian.net")
    project = os.getenv("JIRA_PROJECT_KEY", "ESB")

    # Normalize the ticket ID (STT often mangles numbers)
    ticket_id = _normalize_ticket_id(ticket_id)
    alog(f"JIRA lookup: {ticket_id}")

    async def _try_fetch(tid: str) -> Optional[dict]:
        try:
            url = f"{base}/rest/api/3/issue/{tid}?fields=summary,status,assignee,priority,description"
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Basic {auth}", "Accept": "application/json"},
                    timeout=10,
                )
            alog(f"JIRA {tid} → HTTP {resp.status_code}")
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
            else:
                alog(f"JIRA FAIL {tid}: {resp.status_code} {resp.text[:100]}")
                logger.warning(f"Jira {tid}: {resp.status_code} {resp.text[:100]}")
        except Exception as e:
            alog(f"JIRA EXC {tid}: {e}")
            logger.error(f"Jira fetch error for {tid}: {e}")
        return None

    # Try the normalized ID first
    result = await _try_fetch(ticket_id)
    if result:
        return result

    # Fallback: if the number is short (e.g., "399"), try common prefixes
    # STT often drops leading digits: "1399" → "30. 99" → "3099" or "399"
    digits = re.sub(r"[^0-9]", "", ticket_id)
    if digits and len(digits) <= 3:
        # Try with "1" prefix (most common: 1399 heard as 399)
        for prefix in ["1", "2"]:
            fallback_id = f"{project}-{prefix}{digits}"
            alog(f"JIRA fallback: {fallback_id}")
            result = await _try_fetch(fallback_id)
            if result:
                return result

    return {"error": f"Ticket {ticket_id} not found. Ask them to repeat the ticket number."}


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

    # Add user message to conversation
    user_msg = {"role": "user", "content": f'{speaker} says: "{transcript}"'}
    conversation.append(user_msg)

    # Keep conversation manageable — only real exchanges (no "..." junk)
    history = conversation[-20:]

    client = get_anthropic()
    msg = await client.messages.create(
        model="claude-haiku-4-5-20251001",
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
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=history,
        )

    # Extract text
    response = " ".join(
        block.text for block in msg.content if hasattr(block, "text")
    ).strip()

    # Only save REAL responses to history — NOT silence ("...")
    # Silence pollutes history and makes Claude think it should keep being silent
    if response and response.strip() not in ("...", "…", ""):
        conversation.append({"role": "assistant", "content": response})
    else:
        # Remove the user message too — don't clutter history with unanswered messages
        if conversation and conversation[-1] == user_msg:
            conversation.pop()

    return response


# ── Audio processing ────────────────────────────────────────────────────────────

async def process_audio_chunk(bot_id: str, pcm: bytes) -> None:
    """STT → trigger check → Claude → TTS → queue audio for Meeting BaaS."""
    try:
        return await _process_audio_chunk_inner(bot_id, pcm)
    except Exception as e:
        alog(f"CHUNK EXC: {e}")
        logger.error(f"process_audio_chunk error: {e}")

async def _process_audio_chunk_inner(bot_id: str, pcm: bytes) -> None:
    global speaking_until, _flush_task

    # ── Echo suppression: skip audio while Max is speaking ──
    if time.time() < speaking_until:
        alog(f"ECHO SKIP (Max still speaking, {speaking_until - time.time():.1f}s left)")
        return

    transcript = await pcm_to_text(pcm)
    if not transcript:
        return

    logger.info(f"🎤 [{bot_id}] {transcript[:100]}")

    # Track recent transcripts for debugging
    recent_transcripts.append({
        "text":      transcript[:200],
        "at":        time.strftime("%H:%M:%S"),
    })
    if len(recent_transcripts) > 20:
        recent_transcripts.pop(0)

    alog(f"STT: {transcript[:50]!r}")

    # ── Accumulate fragments and wait for pause before sending to Claude ──
    # This prevents "Can you give me" + "your updates" from being sent as 2 separate messages.
    transcript_fragments.append(transcript)

    # Cancel any pending flush — speaker is still talking
    if _flush_task and not _flush_task.done():
        _flush_task.cancel()

    # Schedule a flush after ACCUMULATOR_PAUSE seconds of silence
    _flush_task = asyncio.create_task(_flush_accumulated(bot_id))


def _clean_transcript(text: str) -> str:
    """Fix common STT artifacts, especially fragmented numbers and name mishearings.
    '30. 99.' → '3099', 'ticket number 30. 99' → 'ticket number 3099'
    'Hey, Mike' → 'Hey, Max' (STT consistently hears Max as Mike/Mack/Next/Mark)
    """
    # Fix Max name mishearings — STT consistently gets these wrong
    # Must be done BEFORE other cleanup to preserve sentence structure
    text = re.sub(r"\bMike\b", "Max", text, flags=re.IGNORECASE)
    text = re.sub(r"\bMack\b", "Max", text, flags=re.IGNORECASE)
    text = re.sub(r"\bMarks?\b", "Max", text, flags=re.IGNORECASE)
    text = re.sub(r"\bMacs?\b", "Max", text, flags=re.IGNORECASE)
    text = re.sub(r"\bAmex\b", "Max", text, flags=re.IGNORECASE)
    text = re.sub(r"\bNext\b", "Max", text, flags=re.IGNORECASE)
    # Remove standalone single-letter fragments like "K." "A." "Space."
    text = re.sub(r"\b[A-Z]\.\s*", "", text)
    # Remove "Space." artifacts
    text = re.sub(r"\bSpace\.\s*", "", text, flags=re.IGNORECASE)
    # Join fragmented numbers: "30. 99" → "3099", "13. 99" → "1399"
    text = re.sub(r"(\d+)\.\s+(\d+)", r"\1\2", text)
    # Clean up multiple spaces
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def _flush_accumulated(bot_id: str) -> None:
    """Wait for a pause, then send all accumulated fragments to Claude as one message."""
    global speaking_until
    await asyncio.sleep(ACCUMULATOR_PAUSE)

    if not transcript_fragments:
        return

    # Join all fragments into one complete message and clean up STT artifacts
    full_transcript = _clean_transcript(" ".join(transcript_fragments))
    transcript_fragments.clear()

    alog(f"FLUSH to Claude: {full_transcript[:80]!r}")
    response = await claude_respond("Team", full_transcript)
    if not response or response.strip() in ("...", "…", ""):
        alog(f"SILENT (Claude chose not to respond)")
        return

    logger.info(f"🤖 Max: {response[:100]}")
    alog(f"RESPONSE TTS: {response[:60]!r}")
    audio_pcm = await text_to_pcm(response)
    if audio_pcm:
        queue = audio_input_queues.get(bot_id)
        if queue:
            frame_size = int(SAMPLE_RATE * 0.1 * 2)
            n_frames = -(-len(audio_pcm) // frame_size)
            # Set echo suppression: block incoming audio while Max speaks + 1.5s buffer
            speaking_until = time.time() + (n_frames * 0.1) + 1.5
            for i in range(0, len(audio_pcm), frame_size):
                await queue.put(audio_pcm[i:i + frame_size])
            alog(f"QUEUED {len(audio_pcm):,}B in {n_frames} frames for {bot_id} (echo guard {n_frames*0.1+1.5:.1f}s)")
            logger.info(f"🔊 Queued {len(audio_pcm):,} bytes in {n_frames} frames")
        else:
            alog(f"QUEUE MISS — no ws connected for bot_id={bot_id} (keys={list(audio_input_queues.keys())})")


# ── WebSocket: bidirectional audio with Meeting BaaS ───────────────────────────

@app.websocket("/ws/{bot_id}")
async def ws_bidirectional(websocket: WebSocket, bot_id: str):
    """Single bidirectional WebSocket endpoint — Meeting BaaS connects here.
    - Meeting BaaS SENDS meeting audio TO us  → we forward to STT → Claude
    - We SEND TTS audio TO Meeting BaaS       → plays in Google Meet
    Both directions on the same WebSocket connection (receive_loop + send_loop run concurrently).
    """
    await websocket.accept()
    send_queue: asyncio.Queue = asyncio.Queue()
    audio_input_queues[bot_id] = send_queue
    audio_buffers[bot_id] = b""
    alog(f"WS/BIDIR connected bot_id={bot_id}")
    logger.info(f"🎙️  Bidirectional stream connected — bot: {bot_id}")

    # Greet the team once audio is confirmed working.
    async def _greet():
        global speaking_until, greeting_sent
        try:
            # Only greet once — prevents double greeting if multiple bots join
            if greeting_sent:
                alog("GREET skipped — already greeted")
                return
            greeting_sent = True
            # Wait 10s for MBaaS to fully establish the audio stream
            await asyncio.sleep(10.0)
            alog("GREET generating...")
            greeting_text = "Hey team! Max here, ready to crush some testing today!"
            audio_pcm = await text_to_pcm(greeting_text)
            if audio_pcm:
                frame_size = int(SAMPLE_RATE * 0.1 * 2)
                n = -(-len(audio_pcm) // frame_size)
                speaking_until = time.time() + (n * 0.1) + 2.0
                for i in range(0, len(audio_pcm), frame_size):
                    await send_queue.put(audio_pcm[i:i + frame_size])
                alog(f"GREET queued {len(audio_pcm):,}B in {n} frames")
                # Also add to conversation so Claude knows Max already greeted
                conversation.append({"role": "assistant", "content": greeting_text})
            else:
                alog("GREET FAILED — TTS returned empty")
        except Exception as e:
            alog(f"GREET EXC: {e}")
            logger.error(f"Greeting error: {e}")

    asyncio.create_task(_greet())

    async def receive_loop():
        """Receive meeting audio from Meeting BaaS and buffer for STT."""
        while True:
            msg = await websocket.receive()
            # Binary frame → raw PCM
            if "bytes" in msg and msg["bytes"]:
                audio_buffers[bot_id] += msg["bytes"]
            # Text frame → JSON envelope with base64 audio
            elif "text" in msg and msg["text"]:
                try:
                    data = json.loads(msg["text"])
                    raw = data.get("data") or data.get("audio") or ""
                    if raw:
                        audio_buffers[bot_id] += base64.b64decode(raw)
                except Exception as e:
                    logger.warning(f"WS text parse error: {e}")
            # Flush buffer to STT when we have 1 second of audio
            if len(audio_buffers[bot_id]) >= CHUNK_SIZE:
                chunk = audio_buffers[bot_id]
                audio_buffers[bot_id] = b""
                asyncio.create_task(process_audio_chunk(bot_id, chunk))

    async def send_loop():
        """Send TTS audio (or silence keepalive) to Meeting BaaS at real-time pace.
        One frame per 100ms tick — MUST sleep after each send for real-time pacing."""
        while True:
            try:
                audio_pcm = send_queue.get_nowait()
                await websocket.send_bytes(audio_pcm)
                alog(f"SENT {len(audio_pcm):,}B to MBaaS")
            except asyncio.QueueEmpty:
                # No speech queued — send silence to keep the stream alive
                await websocket.send_bytes(SILENCE_FRAME)
            await asyncio.sleep(0.1)  # real-time pacing: 100ms per frame — DO NOT REMOVE

    recv_task = asyncio.create_task(receive_loop())
    send_task = asyncio.create_task(send_loop())
    try:
        # Run both loops concurrently — clean up as soon as either ends (disconnect)
        await asyncio.wait(
            [recv_task, send_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
    except Exception as e:
        logger.error(f"ws_bidirectional error: {e}")
    finally:
        recv_task.cancel()
        send_task.cancel()
        audio_input_queues.pop(bot_id, None)
        audio_buffers.pop(bot_id, None)
        logger.info(f"🔌 Bidirectional stream disconnected — bot: {bot_id}")


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

    # v2 API (from SDK v6.0.5): streaming_enabled MUST be true (defaults false!),
    # and streaming config uses NESTED streaming_config object.
    # input_url = where MBaaS reads our TTS audio to play in meeting.
    # output_url = where MBaaS sends meeting audio for our STT.
    # audio_frequency = integer Hz. Supported: 24000 (default), 32000, 48000.
    ws_url = f"wss://{domain}/ws/max"
    payload = {
        "bot_name":          bot_name,
        "meeting_url":       meeting_url,
        "recording_mode":    "audio_only",
        "streaming_enabled": True,
        "streaming_config": {
            "input_url":       ws_url,
            "output_url":      ws_url,
            "audio_frequency": SAMPLE_RATE,
        },
        "webhook_url":       f"https://{domain}/webhook",
        "extra":             {},
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


# ── Jira test (temporary diagnostic) ───────────────────────────────────────────

@app.get("/jira-test/{ticket_id}")
async def jira_test(ticket_id: str):
    """Direct Jira API test — bypasses Claude/STT to test auth directly."""
    auth = _jira_auth()
    if not auth:
        return {"error": "No auth — JIRA_EMAIL or JIRA_API_TOKEN missing"}
    base = os.getenv("JIRA_URL", "https://everperform.atlassian.net")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{base}/rest/api/3/issue/{ticket_id}?fields=summary,status",
                headers={"Authorization": f"Basic {auth}", "Accept": "application/json"},
                timeout=10,
            )
        return {
            "status_code": resp.status_code,
            "body": resp.json() if resp.status_code == 200 else resp.text[:300],
            "auth_length": len(auth),
        }
    except Exception as e:
        return {"error": str(e)}


# ── Debug ───────────────────────────────────────────────────────────────────────

@app.get("/debug")
async def debug():
    """Shows recent STT transcripts — use to verify audio pipeline is working."""
    # Check Jira token health (don't expose the token itself)
    jira_token = os.getenv("JIRA_API_TOKEN", "")
    return {
        "active_streams":     len(audio_input_queues),
        "active_buffers":     {k: len(v) for k, v in audio_buffers.items()},
        "recent_transcripts": recent_transcripts[-10:],
        "conversation_turns": len(conversation),
        "audio_log":          audio_log[-50:],
        "sample_rate":        SAMPLE_RATE,
        "jira_token_length":  len(jira_token),
        "jira_token_ends":    jira_token[-10:] if jira_token else "NOT SET",
        "as_of":              time.strftime("%Y-%m-%d %H:%M:%S IST"),
    }


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
