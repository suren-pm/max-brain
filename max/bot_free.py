"""
Max's brain — FREE TIER version.

This uses entirely free services:
- Google Gemini 2.5 Flash (free tier: 250 requests/day)
- Deepgram STT (free: $200 credit on signup)
- Kokoro TTS (free: runs locally, no API key needed)
- Daily.co transport (free: 10,000 minutes/month)

Total cost: $0/month

Pipeline: Audio In → Deepgram STT → Gemini Flash → Kokoro TTS → Audio Out
"""

import asyncio
import os
from datetime import datetime

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    EndFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.transports.services.daily import DailyParams, DailyTransport

from max.persona import SYSTEM_PROMPT
from max.notes import MeetingNoteTaker


async def create_max_bot_free(
    room_url: str,
    token: str = None,
    bot_name: str = "Max",
    jira_context: str = None,
):
    """Create Max using 100% free services.

    Args:
        room_url: Daily room URL to join
        token: Daily room token (optional)
        bot_name: Display name in the meeting
        jira_context: Pre-fetched Jira sprint context
    """

    # --- Transport: Daily.co (FREE — 10,000 min/month) ---
    transport = DailyTransport(
        room_url,
        token,
        bot_name,
        DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_enabled=True,
            vad_audio_passthrough=True,
            transcription_enabled=True,
        ),
    )

    # --- Ears: Deepgram (FREE — $200 credit on signup) ---
    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        params=DeepgramSTTService.InputParams(
            model="nova-3",
            language="en",
            smart_format=True,
        ),
    )

    # --- Brain: Google Gemini Flash (FREE — 250 req/day) ---
    full_system_prompt = SYSTEM_PROMPT
    if jira_context:
        full_system_prompt += f"\n\n{jira_context}"
    full_system_prompt += f"\n\nToday's date: {datetime.now().strftime('%A, %d %B %Y')}"

    llm = GoogleLLMService(
        api_key=os.getenv("GOOGLE_API_KEY"),
        model="gemini-2.5-flash",
        params=GoogleLLMService.InputParams(
            max_tokens=300,
            temperature=0.7,
        ),
    )

    # --- Voice: Kokoro TTS (FREE — runs locally, no API key!) ---
    tts = KokoroTTSService(
        params=KokoroTTSService.InputParams(
            # Kokoro runs locally with ONNX — no API key needed
            # Voice options: af_heart, af_bella, am_adam, am_michael, bf_emma, bm_george
            voice="bm_george",  # British male — professional voice for Max
            speed=1.0,
        ),
    )

    # --- Note Taker ---
    note_taker = MeetingNoteTaker()

    # --- Conversation Context ---
    messages = [
        {"role": "system", "content": full_system_prompt},
        {
            "role": "user",
            "content": "You've just joined the daily standup meeting. Stay quiet and listen "
                       "until someone addresses you or asks for your update. When it's your turn, "
                       "give a brief standup update. Remember: be concise and natural.",
        },
    ]

    context = OpenAILLMContext(messages)
    context_aggregator = llm.create_context_aggregator(context)

    # --- Pipeline ---
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            note_taker,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
        ),
    )

    # --- Event Handlers ---
    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        logger.info(f"Participant joined: {participant.get('info', {}).get('userName', 'unknown')}")
        await task.queue_frames([context_aggregator.user().get_context_frame()])

    @transport.event_handler("on_participant_joined")
    async def on_participant_joined(transport, participant):
        name = participant.get("info", {}).get("userName", "someone")
        logger.info(f"📥 {name} joined the meeting")
        note_taker.add_event(f"{name} joined the meeting")

    @transport.event_handler("on_participant_left")
    async def on_participant_left(transport, participant, reason):
        name = participant.get("info", {}).get("userName", "someone")
        logger.info(f"📤 {name} left the meeting")
        note_taker.add_event(f"{name} left the meeting")

    @transport.event_handler("on_call_state_updated")
    async def on_call_state_updated(transport, state):
        if state == "left":
            logger.info("Max has left the meeting")
            notes = note_taker.compile_notes()
            if notes:
                notes_path = f"standup_notes_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
                with open(notes_path, "w") as f:
                    f.write(notes)
                logger.info(f"📝 Meeting notes saved to {notes_path}")
            await task.queue_frames([EndFrame()])

    # --- Run ---
    runner = PipelineRunner()
    logger.info(f"🤖 Max (FREE mode) is joining {room_url}")
    logger.info("💰 Using: Gemini Flash (free) + Deepgram (free credit) + Kokoro (local) + Daily (free)")
    await runner.run(task)

    return note_taker.compile_notes()
