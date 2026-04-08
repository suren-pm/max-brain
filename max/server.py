"""
Max's Railway Brain Server — Pipecat Streaming Architecture
=============================================================
Architecture (following Meeting BaaS official speaking-bot pattern):

  Google Meet
      ↕ (audio)
  Meeting BaaS
      ↕ (raw PCM via WebSocket)
  /ws/{bot_id}  ←→  Router  ←→  /pipecat/{bot_id}
                  (raw↔protobuf)      ↕
                              Pipecat Pipeline:
                                Silero VAD
                                Deepgram Streaming STT
                                Claude (Anthropic)
                                Deepgram Streaming TTS

Two WebSocket endpoints (same pattern as official Meeting BaaS speaking-bot):
  /ws/{bot_id}      — Meeting BaaS connects here (raw PCM audio, bidirectional)
  /pipecat/{bot_id} — Pipecat pipeline connects here (protobuf frames, bidirectional)

Router bridges raw PCM ↔ protobuf between the two WebSocket connections.
Pipecat runs in-process as an asyncio task (no subprocess needed on Railway).

Endpoints:
  POST /join           → trigger Max to join a meeting
  WS   /ws/{bot_id}    → Meeting BaaS raw audio
  WS   /pipecat/{bot_id} → Pipecat protobuf frames
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

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
import sys

# Capture ALL loguru output (including Pipecat internals) to diag_log
# This lets us see Deepgram/Claude/Pipecat errors via /debug
_pipecat_logs: list[str] = []

def _log_sink(message):
    """Custom loguru sink that captures log messages for /debug endpoint."""
    text = str(message).strip()
    # Only capture important pipecat/service messages, not spam
    if any(kw in text.lower() for kw in ["error", "exception", "fail", "connect", "disconnect",
                                           "audio", "stt", "tts", "llm", "anthropic", "deepgram",
                                           "pipeline", "frame", "vad", "transport", "greet",
                                           "websocket", "sample", "running", "start", "stop",
                                           "close", "timeout", "queue", "processing"]):
        ts = time.strftime('%H:%M:%S')
        entry = f"{ts} [PC] {text[-200:]}"
        _pipecat_logs.append(entry)
        if len(_pipecat_logs) > 50:
            _pipecat_logs.pop(0)

logger.add(_log_sink, level="DEBUG")

app = FastAPI(title="Max Brain Server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Constants ──────────────────────────────────────────────────────────────────
MEETING_BAAS_API = "https://api.meetingbaas.com/v2"
SAMPLE_RATE      = 24000

# ── In-memory state ───────────────────────────────────────────────────────────
pending_tasks:  list[dict] = []
test_results:   list[dict] = []
briefing_cache: str        = ""
standup_notes:  list[dict] = []  # Max's live standup notes

# ── Connection registry (MBaaS ↔ Pipecat bridge) ─────────────────────────────
client_connections: dict[str, WebSocket] = {}   # bot_id → MBaaS WebSocket
pipecat_connections: dict[str, WebSocket] = {}  # bot_id → Pipecat WebSocket
closing_clients: set[str] = set()
active_pipelines: dict[str, dict] = {}
audio_ready_events: dict[str, asyncio.Event] = {}  # signals when MBaaS audio is flowing

# ── Diagnostic logging ────────────────────────────────────────────────────────
diag_log: list[str] = []

def alog(msg: str) -> None:
    ts = time.strftime('%H:%M:%S')
    entry = f"{ts} {msg}"
    diag_log.append(entry)
    if len(diag_log) > 100:
        diag_log.pop(0)
    logger.info(msg)


# ── Protobuf converter (raw PCM ↔ Pipecat protobuf frames) ───────────────────
# Uses Pipecat's INTERNAL protobuf module (pipecat.frames.protobufs.frames_pb2)
# to avoid duplicate proto registration errors.

_frame_protos = None

def _get_protos():
    global _frame_protos
    if _frame_protos is None:
        try:
            # Pipecat >= 0.0.50 path
            import pipecat.frames.protobufs.frames_pb2 as fp
            _frame_protos = fp
            alog("PROTO: loaded from pipecat.frames.protobufs.frames_pb2")
        except ImportError:
            try:
                # Older Pipecat path
                import pipecat.serializers.protobuf_serializer as ps
                _frame_protos = ps.frame_protos
                alog("PROTO: loaded from pipecat.serializers (fallback)")
            except ImportError as e:
                alog(f"PROTO IMPORT FAILED: {e}")
                raise
    return _frame_protos

def raw_to_protobuf(raw_audio: bytes) -> bytes:
    """Wrap raw PCM audio in a Pipecat protobuf AudioRawFrame."""
    fp = _get_protos()
    frame = fp.Frame()
    frame.audio.audio = raw_audio
    frame.audio.sample_rate = SAMPLE_RATE
    frame.audio.num_channels = 1
    return frame.SerializeToString()

def protobuf_to_raw(proto_data: bytes) -> Optional[bytes]:
    """Extract raw PCM audio from a Pipecat protobuf frame."""
    fp = _get_protos()
    frame = fp.Frame()
    frame.ParseFromString(proto_data)
    if frame.HasField("audio"):
        return bytes(frame.audio.audio)
    return None


# ── Jira helpers ──────────────────────────────────────────────────────────────

def _jira_auth() -> Optional[str]:
    email = os.getenv("JIRA_EMAIL")
    token = os.getenv("JIRA_API_TOKEN")
    if not (email and token):
        return None
    return base64.b64encode(f"{email}:{token}".encode()).decode()

def _normalize_ticket_id(raw: str) -> str:
    raw = raw.strip().upper()
    if re.match(r"[A-Z]+-\d+", raw):
        return raw
    digits = re.sub(r"[^0-9]", "", raw)
    if digits:
        project = os.getenv("JIRA_PROJECT_KEY", "ESB")
        return f"{project}-{digits}"
    return raw

async def jira_get_ticket(ticket_id: str) -> dict:
    auth = _jira_auth()
    if not auth:
        return {"error": "Jira not configured"}
    base = os.getenv("JIRA_URL", "https://everperform.atlassian.net")
    project = os.getenv("JIRA_PROJECT_KEY", "ESB")
    ticket_id = _normalize_ticket_id(ticket_id)
    alog(f"JIRA lookup: {ticket_id}")

    async def _try_fetch(tid: str) -> Optional[dict]:
        try:
            url = f"{base}/rest/api/3/issue/{tid}?fields=summary,status,assignee,priority"
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
        except Exception as e:
            alog(f"JIRA EXC {tid}: {e}")
        return None

    result = await _try_fetch(ticket_id)
    if result:
        return result
    # Fallback for short numbers (STT drops leading digits)
    digits = re.sub(r"[^0-9]", "", ticket_id)
    if digits and len(digits) <= 3:
        for prefix in ["1", "2"]:
            fallback_id = f"{project}-{prefix}{digits}"
            result = await _try_fetch(fallback_id)
            if result:
                return result
    return {"error": f"Ticket {ticket_id} not found. Ask them to repeat the number."}

async def jira_testing_tickets() -> list[dict]:
    auth = _jira_auth()
    if not auth:
        return []
    base = os.getenv("JIRA_URL", "https://everperform.atlassian.net")
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


# ── Pipecat Pipeline (runs in-process as asyncio task) ────────────────────────

async def run_pipecat_pipeline(bot_id: str):
    """Run the Pipecat streaming pipeline.

    Connects to ws://localhost:{PORT}/pipecat/{bot_id} as a WebSocket client.
    The /pipecat endpoint bridges to /ws/{bot_id} where Meeting BaaS is connected.
    """
    try:
        return await _run_pipecat_pipeline_inner(bot_id)
    except Exception as e:
        import traceback
        alog(f"PIPELINE FATAL: {e}")
        logger.error(f"Pipeline fatal error: {traceback.format_exc()}")

async def _run_pipecat_pipeline_inner(bot_id: str):
    """Inner pipeline function — separated so errors are always caught."""
    alog(f"PIPELINE init — importing pipecat modules...")
    try:
        from pipecat.audio.vad.silero import SileroVADAnalyzer, VADParams
        from pipecat.frames.frames import LLMMessagesFrame
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.runner import PipelineRunner
        from pipecat.pipeline.task import PipelineParams, PipelineTask
        from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
        from pipecat.serializers.protobuf import ProtobufFrameSerializer
        from pipecat.services.anthropic.llm import AnthropicLLMService
        from pipecat.services.deepgram.stt import DeepgramSTTService
        from pipecat.services.deepgram.tts import DeepgramTTSService
        # Try both import paths for WebSocket transport (changed between versions)
        try:
            from pipecat.transports.websocket.client import (
                WebsocketClientParams,
                WebsocketClientTransport,
            )
            alog("IMPORT: websocket transport from pipecat.transports.websocket.client")
        except ImportError:
            from pipecat.transports.network.websocket_client import (
                WebsocketClientParams,
                WebsocketClientTransport,
            )
            alog("IMPORT: websocket transport from pipecat.transports.network.websocket_client (fallback)")
        from pipecat.services.llm_service import FunctionCallParams
        from pipecat.adapters.schemas.function_schema import FunctionSchema
        from pipecat.adapters.schemas.tools_schema import ToolsSchema
        from max.persona import SYSTEM_PROMPT
        alog("PIPELINE imports OK")
    except ImportError as e:
        alog(f"PIPELINE IMPORT ERROR: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return

    try:
        from pipecat.utils.asyncio import TaskManager
        TaskManager.set_event_loop(TaskManager, asyncio.get_running_loop())
        alog("PIPELINE TaskManager event loop set")
    except Exception as e:
        alog(f"PIPELINE TaskManager error (non-fatal): {e}")

    port = os.getenv("PORT", "8080")
    pipecat_ws_url = f"ws://localhost:{port}/pipecat/{bot_id}"
    alog(f"PIPECAT connecting to {pipecat_ws_url}")

    # ── Transport ──
    transport = WebsocketClientTransport(
        uri=pipecat_ws_url,
        params=WebsocketClientParams(
            audio_out_sample_rate=SAMPLE_RATE,
            audio_out_enabled=True,
            add_wav_header=False,
            audio_in_enabled=True,
            vad_analyzer=SileroVADAnalyzer(
                sample_rate=16000,
                params=VADParams(
                    threshold=0.4,
                    min_speech_duration_ms=200,
                    min_silence_duration_ms=400,
                    min_volume=0.2,
                ),
            ),
            serializer=ProtobufFrameSerializer(),
            timeout=300,
        ),
    )

    # ── STT: Deepgram streaming WebSocket ──
    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        encoding="linear16",
        sample_rate=SAMPLE_RATE,
        language="en",
    )

    # ── LLM: Claude ──
    llm = AnthropicLLMService(
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        model="claude-haiku-4-5-20251001",
    )

    # ── Register tool functions ──
    async def tool_get_jira_ticket(params: FunctionCallParams):
        result = await jira_get_ticket(params.arguments.get("ticket_id", ""))
        await params.result_callback(json.dumps(result))

    async def tool_get_testing_tickets(params: FunctionCallParams):
        tickets = await jira_testing_tickets()
        await params.result_callback(json.dumps(tickets))

    async def tool_log_task(params: FunctionCallParams):
        task = {
            "ticket_id":   params.arguments.get("ticket_id"),
            "description": params.arguments.get("description", ""),
            "logged_at":   time.strftime("%Y-%m-%d %H:%M IST"),
            "status":      "pending",
        }
        pending_tasks.append(task)
        alog(f"TASK logged: {task['ticket_id']}")
        await params.result_callback(json.dumps({"ok": True, "task": task}))

    async def tool_get_test_results(params: FunctionCallParams):
        await params.result_callback(json.dumps({
            "results": test_results[-10:],
            "pending": [t for t in pending_tasks if t.get("status") == "pending"],
        }))

    async def tool_get_standup_briefing(params: FunctionCallParams):
        await params.result_callback(json.dumps({
            "briefing":        briefing_cache or "No briefing available.",
            "completed_tests": [r for r in test_results if r][-5:],
            "pending_tasks":   [t for t in pending_tasks if t.get("status") == "pending"],
        }))

    async def tool_save_standup_note(params: FunctionCallParams):
        note = {
            "speaker":    params.arguments.get("speaker", "unknown"),
            "summary":    params.arguments.get("summary", ""),
            "action_items": params.arguments.get("action_items", ""),
            "timestamp":  time.strftime("%H:%M IST"),
        }
        standup_notes.append(note)
        alog(f"NOTE saved: {note['speaker']} — {note['summary'][:60]}")
        await params.result_callback(json.dumps({"ok": True, "note": note, "total_notes": len(standup_notes)}))

    llm.register_function("get_jira_ticket", tool_get_jira_ticket)
    llm.register_function("get_testing_tickets", tool_get_testing_tickets)
    llm.register_function("log_task", tool_log_task)
    llm.register_function("get_test_results", tool_get_test_results)
    llm.register_function("get_standup_briefing", tool_get_standup_briefing)
    llm.register_function("save_standup_note", tool_save_standup_note)

    # ── Tool schemas ──
    tools = ToolsSchema(standard_tools=[
        FunctionSchema(
            name="get_jira_ticket",
            description="Fetch details of a Jira ticket by ID (e.g. ESB-1275).",
            properties={
                "ticket_id": {"type": "string", "description": "Jira ticket ID like ESB-1275 or just the number"}
            },
            required=["ticket_id"],
        ),
        FunctionSchema(
            name="get_testing_tickets",
            description="Get all Jira tickets currently in Testing status.",
            properties={},
            required=[],
        ),
        FunctionSchema(
            name="log_task",
            description="Log a task assigned to you in standup for testing.",
            properties={
                "ticket_id":   {"type": "string", "description": "Jira ticket ID"},
                "description": {"type": "string", "description": "What needs to be tested"},
            },
            required=["ticket_id"],
        ),
        FunctionSchema(
            name="get_test_results",
            description="Get test results posted by the testing agent.",
            properties={},
            required=[],
        ),
        FunctionSchema(
            name="get_standup_briefing",
            description="Get standup briefing with completed tests and pending tasks.",
            properties={},
            required=[],
        ),
        FunctionSchema(
            name="save_standup_note",
            description="Save a note from what someone said in standup. Use this to capture updates, blockers, and action items as people give their updates.",
            properties={
                "speaker":      {"type": "string", "description": "Who is speaking (e.g. 'Suren', 'Dev Team')"},
                "summary":      {"type": "string", "description": "Brief summary of what they said"},
                "action_items": {"type": "string", "description": "Any action items or follow-ups mentioned (empty string if none)"},
            },
            required=["speaker", "summary"],
        ),
    ])

    # ── TTS: Deepgram streaming WebSocket ──
    tts = DeepgramTTSService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        voice="aura-arcas-en",
        sample_rate=SAMPLE_RATE,
    )

    # ── Context ──
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    context = OpenAILLMContext(messages, tools)
    aggregator_pair = llm.create_context_aggregator(context)

    # ── Pipeline ──
    pipeline = Pipeline([
        transport.input(),
        stt,
        aggregator_pair.user(),
        llm,
        tts,
        aggregator_pair.assistant(),
        transport.output(),
    ])
    alog("PIPELINE built: transport→STT→Claude→TTS→transport")

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            check_dangling_tasks=True,
        ),
    )

    # ── Greeting: wait for audio bridge to be ready, then greet ──
    async def send_greeting():
        evt = audio_ready_events.get(bot_id)
        if evt:
            try:
                await asyncio.wait_for(evt.wait(), timeout=15)
                alog("GREETING: audio bridge confirmed ready")
            except asyncio.TimeoutError:
                alog("GREETING: timeout waiting for audio, greeting anyway")
        # Extra buffer for MBaaS to fully establish bidirectional audio
        await asyncio.sleep(4)
        alog("GREETING queued via LLMMessagesFrame")
        try:
            # Include system prompt — LLMMessagesFrame bypasses context aggregator
            await task.queue_frame(LLMMessagesFrame([
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "You just joined the standup meeting. Introduce yourself briefly with energy!"},
            ]))
            alog("GREETING frame queued OK")
        except Exception as e:
            alog(f"GREETING ERROR: {e}")

    asyncio.create_task(send_greeting())

    # ── Run ──
    runner = PipelineRunner()
    active_pipelines[bot_id] = {"status": "running", "started": time.strftime("%H:%M:%S")}
    try:
        alog(f"PIPELINE running for {bot_id}")
        await runner.run(task)
    except Exception as e:
        alog(f"PIPELINE ERROR: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        active_pipelines.pop(bot_id, None)
        alog(f"PIPELINE ended for {bot_id}")


# ── WebSocket: Meeting BaaS raw audio ─────────────────────────────────────────

@app.websocket("/ws/{bot_id}")
async def ws_meetingbaas(websocket: WebSocket, bot_id: str):
    """Meeting BaaS connects here — sends/receives raw PCM audio."""
    await websocket.accept()
    client_connections[bot_id] = websocket
    audio_ready_events[bot_id] = asyncio.Event()
    alog(f"WS/MBaaS connected: {bot_id}")
    audio_chunks_in = 0

    # Start the Pipecat pipeline (connects to /pipecat/{bot_id})
    # Add done callback to surface any uncaught exceptions
    def _pipeline_done(t):
        if t.exception():
            alog(f"PIPELINE TASK EXCEPTION: {t.exception()}")
            logger.error(f"Pipeline task exception: {t.exception()}")
    pipeline_task = asyncio.create_task(run_pipecat_pipeline(bot_id))
    pipeline_task.add_done_callback(_pipeline_done)

    try:
        while True:
            msg = await websocket.receive()
            if bot_id in closing_clients:
                break

            raw_audio = None
            if "bytes" in msg and msg["bytes"]:
                raw_audio = msg["bytes"]
            elif "text" in msg and msg["text"]:
                try:
                    data = json.loads(msg["text"])
                    raw = data.get("data") or data.get("audio") or ""
                    if raw:
                        raw_audio = base64.b64decode(raw)
                except Exception:
                    pass

            if raw_audio:
                audio_chunks_in += 1
                # Signal audio is flowing after 10 chunks (~1s of audio)
                if audio_chunks_in == 10:
                    evt = audio_ready_events.get(bot_id)
                    if evt:
                        evt.set()
                        alog(f"AUDIO READY: MBaaS audio flowing for {bot_id}")
                # Log first few chunks and then every 100th
                if audio_chunks_in <= 3 or audio_chunks_in % 100 == 0:
                    alog(f"BRIDGE MBaaS→Pipecat: chunk #{audio_chunks_in} ({len(raw_audio)} bytes)")
                # Forward to Pipecat as protobuf frame
                pipecat_ws = pipecat_connections.get(bot_id)
                if pipecat_ws:
                    try:
                        proto = raw_to_protobuf(raw_audio)
                        await pipecat_ws.send_bytes(proto)
                    except Exception as e:
                        alog(f"BRIDGE MBaaS→Pipecat ERROR: {e}")
                else:
                    if audio_chunks_in <= 3:
                        alog(f"BRIDGE: MBaaS audio but Pipecat not connected yet (chunk #{audio_chunks_in})")

    except WebSocketDisconnect:
        alog(f"WS/MBaaS disconnected: {bot_id}")
    except Exception as e:
        alog(f"WS/MBaaS error: {e}")
    finally:
        closing_clients.add(bot_id)
        client_connections.pop(bot_id, None)
        audio_ready_events.pop(bot_id, None)
        pipeline_task.cancel()
        try:
            await pipeline_task
        except (asyncio.CancelledError, Exception):
            pass
        closing_clients.discard(bot_id)
        # Post standup notes to callback if any were captured
        if standup_notes:
            asyncio.create_task(_post_meeting_notes_callback())
        alog(f"WS/MBaaS cleanup done: {bot_id}")


# ── WebSocket: Pipecat protobuf frames ────────────────────────────────────────

@app.websocket("/pipecat/{bot_id}")
async def ws_pipecat(websocket: WebSocket, bot_id: str):
    """Pipecat pipeline connects here — sends/receives protobuf frames."""
    await websocket.accept()
    pipecat_connections[bot_id] = websocket
    alog(f"WS/Pipecat connected: {bot_id}")
    audio_chunks_out = 0
    non_audio_frames = 0

    try:
        while True:
            msg = await websocket.receive()
            if bot_id in closing_clients:
                break

            if "bytes" in msg and msg["bytes"]:
                # Pipecat sends protobuf → extract raw audio → forward to MBaaS
                try:
                    audio = protobuf_to_raw(msg["bytes"])
                    if audio:
                        audio_chunks_out += 1
                        if audio_chunks_out <= 3 or audio_chunks_out % 100 == 0:
                            alog(f"BRIDGE Pipecat→MBaaS: chunk #{audio_chunks_out} ({len(audio)} bytes)")
                        client_ws = client_connections.get(bot_id)
                        if client_ws:
                            await client_ws.send_bytes(audio)
                        else:
                            alog(f"BRIDGE Pipecat→MBaaS: no MBaaS connection")
                    else:
                        non_audio_frames += 1
                        if non_audio_frames <= 5:
                            alog(f"BRIDGE Pipecat→MBaaS: non-audio frame (#{non_audio_frames}, {len(msg['bytes'])} bytes)")
                except Exception as e:
                    alog(f"BRIDGE Pipecat→MBaaS ERROR: {e}")

    except WebSocketDisconnect:
        alog(f"WS/Pipecat disconnected: {bot_id}")
    except Exception as e:
        alog(f"WS/Pipecat error: {e}")
    finally:
        pipecat_connections.pop(bot_id, None)
        alog(f"WS/Pipecat cleanup done: {bot_id}")


# ── Join endpoint ─────────────────────────────────────────────────────────────

@app.post("/join")
async def join_meeting(request: Request):
    """Trigger Max to join a Google Meet via Meeting BaaS."""
    body = await request.json()
    meeting_url = body.get("meeting_url") or os.getenv("GOOGLE_MEET_URL", "")
    bot_name = body.get("bot_name", "Max")

    if not meeting_url:
        return {"error": "meeting_url required (or set GOOGLE_MEET_URL env var)"}

    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    if not domain:
        return {"error": "RAILWAY_PUBLIC_DOMAIN not set"}

    # MBaaS will connect to /ws/{bot_id} — we use "max" as the bot_id
    ws_url = f"wss://{domain}/ws/max"
    # MBaaS API — nested streaming object (confirmed from reference speaking-bot repo)
    # No recording_mode = no recording (saves tokens)
    # No speech_to_text = no transcription (saves tokens)
    payload = {
        "bot_name":    bot_name,
        "meeting_url": meeting_url,
        "reserved":    False,
        "streaming": {
            "input":           ws_url,
            "output":          ws_url,
            "audio_frequency": "24khz",
        },
        "webhook_url": f"https://{domain}/webhook",
        "extra":       {},
    }

    api_key = os.getenv("MEETING_BAAS_API_KEY", "")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.meetingbaas.com/v2/bots",
            headers={
                "x-meeting-baas-api-key": api_key,
                "Content-Type":           "application/json",
            },
            json=payload,
            timeout=30,
        )

    if resp.status_code in (200, 201):
        result = resp.json()
        data = result.get("data") or result
        mbass_bot_id = data.get("bot_id") or data.get("id") or result.get("bot_id") or "unknown"
        alog(f"JOIN OK — MBaaS bot_id={mbass_bot_id}")
        return {"ok": True, "bot_id": mbass_bot_id, "meeting_url": meeting_url}

    logger.error(f"Meeting BaaS {resp.status_code}: {resp.text[:200]}")
    return {"error": f"Meeting BaaS {resp.status_code}", "detail": resp.text[:200]}


# ── Task management endpoints ─────────────────────────────────────────────────

@app.post("/tasks/log")
async def post_task(request: Request):
    task = await request.json()
    task.setdefault("logged_at", time.strftime("%Y-%m-%d %H:%M IST"))
    task.setdefault("status", "pending")
    pending_tasks.append(task)
    return {"ok": True, "task": task, "total_pending": len(pending_tasks)}

@app.get("/tasks/log")
async def get_tasks():
    return {
        "tasks":         pending_tasks,
        "pending_count": len([t for t in pending_tasks if t.get("status") == "pending"]),
        "as_of":         time.strftime("%Y-%m-%d %H:%M IST"),
    }

@app.post("/tasks/result")
async def post_result(request: Request):
    result = await request.json()
    result.setdefault("posted_at", time.strftime("%Y-%m-%d %H:%M IST"))
    test_results.append(result)
    for t in pending_tasks:
        if t.get("ticket_id") == result.get("ticket_id"):
            t["status"] = "done"
            break
    return {"ok": True, "result": result}

@app.get("/tasks/results")
async def get_results():
    return {
        "results":  test_results,
        "pending":  [t for t in pending_tasks if t.get("status") == "pending"],
        "done":     [t for t in pending_tasks if t.get("status") == "done"],
        "as_of":    time.strftime("%Y-%m-%d %H:%M IST"),
    }

@app.post("/briefing")
async def post_briefing(request: Request):
    global briefing_cache
    body = await request.json()
    briefing_cache = body.get("briefing", "")
    return {"ok": True}

@app.get("/briefing")
async def get_briefing():
    return {"briefing": briefing_cache, "as_of": time.strftime("%Y-%m-%d %H:%M IST")}


# ── Standup notes endpoints ──────────────────────────────────────────────────

@app.get("/notes")
async def get_notes():
    return {
        "notes":      standup_notes,
        "total":      len(standup_notes),
        "as_of":      time.strftime("%Y-%m-%d %H:%M IST"),
    }

@app.delete("/notes")
async def clear_notes():
    standup_notes.clear()
    return {"ok": True, "message": "Notes cleared"}

@app.get("/notes/summary")
async def get_notes_summary():
    """Get a formatted Slack-ready summary of standup notes."""
    if not standup_notes:
        return {"summary": "No notes captured this standup.", "notes": []}
    lines = [f"*Standup Notes — {time.strftime('%d %b %Y')}*\n"]
    for n in standup_notes:
        line = f"• *{n.get('speaker', '?')}* ({n.get('timestamp', '')}): {n.get('summary', '')}"
        actions = n.get("action_items", "")
        if actions:
            line += f"\n   ↳ _Action: {actions}_"
        lines.append(line)
    return {"summary": "\n".join(lines), "notes": standup_notes}


async def _post_meeting_notes_callback():
    """Called when Max exits a meeting — posts notes to the configured callback."""
    callback_url = os.getenv("NOTES_CALLBACK_URL", "")
    if not callback_url or not standup_notes:
        alog(f"NOTES CALLBACK: skipped (url={'set' if callback_url else 'unset'}, notes={len(standup_notes)})")
        return
    # Build Slack-formatted summary
    lines = [f"*:memo: Max's Standup Notes — {time.strftime('%d %b %Y %H:%M IST')}*\n"]
    for n in standup_notes:
        line = f"• *{n.get('speaker', '?')}* ({n.get('timestamp', '')}): {n.get('summary', '')}"
        actions = n.get("action_items", "")
        if actions:
            line += f"\n   ↳ _Action: {actions}_"
        lines.append(line)
    summary = "\n".join(lines)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(callback_url, json={"text": summary}, timeout=10)
        alog(f"NOTES CALLBACK: posted to webhook — HTTP {resp.status_code}")
    except Exception as e:
        alog(f"NOTES CALLBACK ERROR: {e}")


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.post("/webhook")
async def meeting_baas_webhook(request: Request):
    try:
        event = await request.json()
    except Exception:
        return {"ok": True}
    event_type = event.get("event") or event.get("type", "unknown")
    alog(f"WEBHOOK: {event_type}")
    return {"ok": True}


# ── Debug ─────────────────────────────────────────────────────────────────────

@app.get("/debug")
async def debug():
    # Get pipecat version for diagnostics
    try:
        import pipecat
        pc_version = getattr(pipecat, '__version__', 'unknown')
    except Exception:
        pc_version = 'not installed'

    return {
        "active_pipelines":     active_pipelines,
        "client_connections":   list(client_connections.keys()),
        "pipecat_connections":  list(pipecat_connections.keys()),
        "diag_log":             diag_log[-50:],
        "pipecat_logs":         _pipecat_logs[-30:],
        "pipecat_version":      pc_version,
        "pending_tasks":        len(pending_tasks),
        "test_results":         len(test_results),
        "sample_rate":          SAMPLE_RATE,
        "as_of":                time.strftime("%Y-%m-%d %H:%M:%S IST"),
    }


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status":           "ok",
        "architecture":     "pipecat-streaming",
        "active_pipelines": len(active_pipelines),
        "pending_tasks":    len([t for t in pending_tasks if t.get("status") == "pending"]),
        "test_results":     len(test_results),
        "briefing_ready":   bool(briefing_cache),
    }
