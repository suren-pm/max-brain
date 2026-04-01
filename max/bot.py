"""
Max's brain — the Pipecat pipeline that powers real-time voice interaction.

Pipeline: Meeting audio → Deepgram STT → Claude Brain → Deepgram TTS → Meeting audio out

This module creates the core voice agent pipeline using:
- Deepgram for speech-to-text (hearing)
- Anthropic Claude for reasoning (thinking)
- Deepgram Aura for text-to-speech (speaking)
- Daily transport (Meeting BaaS uses Daily.co internally)
"""

import asyncio
import os
from datetime import datetime

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    LLMMessagesFrame,
    EndFrame,
    TextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.transports.daily.transport import DailyParams, DailyTransport

from max.persona import SYSTEM_PROMPT, get_standup_context
from max.notes import MeetingNoteTaker
from max.context import fetch_jira_context


async def create_max_bot(
    room_url: str,
    token: str = None,
    bot_name: str = "Max",
    jira_context: str = None,
):
    """Create and run Max as a meeting participant.

    Args:
        room_url: Daily room URL to join
        token: Daily room token (optional)
        bot_name: Display name in the meeting
        jira_context: Pre-fetched Jira sprint context
    """
    # --- Transport: How Max joins the meeting ---
    transport = DailyTransport(
        room_url,
        token,
        bot_name,
        DailyParams(
            audio_in_enabled=True,      # Max can hear
            audio_out_enabled=True,     # Max can speak
            vad_enabled=True,           # Voice Activity Detection
            vad_audio_passthrough=True, # Pass audio through during VAD
            transcription_enabled=True, # Enable meeting transcription
        ),
    )

    # --- Ears: Speech-to-Text ---
    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        params=DeepgramSTTService.InputParams(
            model="nova-3",          # Latest Deepgram model
            language="en",
            smart_format=True,       # Punctuation & formatting
        ),
    )

    # --- Brain: Claude ---
    full_system_prompt = SYSTEM_PROMPT
    if jira_context:
        full_system_prompt += f"\n\n{jira_context}"

    # Add today's date context
    full_system_prompt += f"\n\nToday's date: {datetime.now().strftime('%A, %d %B %Y')}"

    llm = AnthropicLLMService(
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        model="claude-sonnet-4-5-20250929",  # Fast + capable for real-time
        params=AnthropicLLMService.InputParams(
            max_tokens=300,          # Keep responses concise for voice
            temperature=0.7,         # Natural but not too random
        ),
    )

    # --- Voice: Text-to-Speech (Deepgram Aura) ---
    tts = DeepgramTTSService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        voice=os.getenv("DEEPGRAM_TTS_VOICE", "aura-arcas-en"),  # Deep, professional male voice
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

    # --- Pipeline: Wire everything together ---
    pipeline = Pipeline(
        [
            transport.input(),                          # Hear meeting audio
            stt,                                        # Convert speech to text
            note_taker,                                 # Record what's being said
            context_aggregator.user(),                  # Track user messages
            llm,                                        # Claude thinks & responds
            tts,                                        # Convert response to speech
            transport.output(),                         # Speak into the meeting
            context_aggregator.assistant(),             # Track assistant messages
        ]
    )

    task = PipelineTask(
        pipeline,
        PipelineParams(
            allow_interruptions=True,        # People can interrupt Max
            enable_metrics=True,             # Track latency
        ),
    )

    # --- Event Handlers ---
    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        """When someone joins, Max is ready but stays quiet."""
        logger.info(f"Participant joined: {participant.get('info', {}).get('userName', 'unknown')}")
        # Don't speak yet — wait to be addressed
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

    @transport.event_handler("on_dialin_ready")
    async def on_dialin_ready(transport, cdata):
        logger.info("Max is connected and ready")

    @transport.event_handler("on_call_state_updated")
    async def on_call_state_updated(transport, state):
        if state == "left":
            logger.info("Max has left the meeting")
            # Save meeting notes
            notes = note_taker.compile_notes()
            if notes:
                notes_path = f"standup_notes_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
                with open(notes_path, "w") as f:
                    f.write(notes)
                logger.info(f"📝 Meeting notes saved to {notes_path}")
            await task.queue_frames([EndFrame()])

    # --- Run Max ---
    runner = PipelineRunner()
    logger.info(f"🤖 Max is joining the meeting at {room_url}")
    await runner.run(task)

    return note_taker.compile_notes()
