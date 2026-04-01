#!/usr/bin/env python3
"""
🤖 Max AI Employee — Run Script

Launch Max to join a meeting as an interactive AI team member.

Usage:
    # FREE mode — $0/month (Gemini + Deepgram free + Kokoro local + Daily free)
    python run.py --free

    # Premium mode — ~$0.07/standup (Claude + Deepgram + Cartesia + Daily)
    python run.py --mode daily

    # Join a Google Meet via Recall.ai (brain server runs on Railway — no Mac required)
    python run.py --mode recall_ai --meeting-url "https://meet.google.com/abc-defg-hij"

    # With Jira context
    python run.py --free --with-jira
"""

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from loguru import logger

# Load environment variables
load_dotenv()

# Configure logging
logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | <level>{message}</level>")
logger.add("max_session_{time}.log", level="DEBUG", rotation="10 MB")


async def main():
    parser = argparse.ArgumentParser(description="Launch Max, the AI Test Engineer")
    parser.add_argument(
        "--mode",
        choices=["daily", "meeting_baas", "recall_ai"],
        default="daily",
        help="How Max joins the meeting (default: daily)",
    )
    parser.add_argument(
        "--free",
        action="store_true",
        help="Use 100%% free stack: Gemini Flash + Deepgram free + Kokoro local TTS",
    )
    parser.add_argument(
        "--meeting-url",
        type=str,
        default=None,
        help="Meeting URL for Google Meet / Zoom / Teams (required for recall_ai / meeting_baas mode)",
    )
    parser.add_argument(
        "--with-jira",
        action="store_true",
        help="Fetch current Jira sprint data before joining",
    )
    parser.add_argument(
        "--bot-name",
        type=str,
        default="Max",
        help="Display name in the meeting (default: Max)",
    )
    args = parser.parse_args()

    # --- Validate environment based on mode ---
    if args.mode == "recall_ai":
        required_keys = ["RECALL_AI_API_KEY", "RAILWAY_SERVER_URL"]
        logger.info("🎥 RECALL.AI MODE — Max joins Google Meet with avatar face (cloud brain on Railway)")
    elif args.free:
        required_keys = ["GOOGLE_API_KEY", "DEEPGRAM_API_KEY"]
        logger.info("💰 FREE MODE — Using Gemini Flash + Deepgram + Kokoro (local) + Daily")
    else:
        required_keys = ["ANTHROPIC_API_KEY", "DEEPGRAM_API_KEY", "CARTESIA_API_KEY"]
        logger.info("💎 PREMIUM MODE — Using Claude + Deepgram + Cartesia + Daily")

    missing = [k for k in required_keys if not os.getenv(k)]
    if missing:
        logger.error(f"Missing required API keys: {', '.join(missing)}")
        logger.error("Copy .env.example to .env and fill in your keys")
        if args.mode == "recall_ai":
            logger.info("For RECALL.AI mode you need:")
            logger.info("  RECALL_AI_API_KEY   — sign up at https://recall.ai")
            logger.info("  RAILWAY_SERVER_URL  — your deployed Railway URL, e.g. https://speaking-meeting-bot-production.up.railway.app")
        elif args.free:
            logger.info("For FREE mode you need:")
            logger.info("  GOOGLE_API_KEY  — free at https://aistudio.google.com/apikey")
            logger.info("  DEEPGRAM_API_KEY — $200 free credit at https://console.deepgram.com")
            logger.info("  DAILY_API_KEY   — free at https://dashboard.daily.co")
        sys.exit(1)

    if args.mode == "daily" and not os.getenv("DAILY_API_KEY"):
        logger.error("DAILY_API_KEY required for daily mode")
        logger.error("Get one FREE at https://dashboard.daily.co")
        sys.exit(1)

    if args.mode == "meeting_baas" and not os.getenv("MEETING_BAAS_API_KEY"):
        logger.error("MEETING_BAAS_API_KEY required for meeting_baas mode")
        sys.exit(1)

    # --- Fetch context ---
    jira_context = None
    if args.with_jira:
        logger.info("📋 Fetching Jira sprint context...")
        from max.context import build_standup_context
        jira_context = await build_standup_context()
        logger.info("✅ Sprint context loaded")

    # --- Recall.ai mode (fully cloud — brain server runs on Railway) ---
    if args.mode == "recall_ai":
        railway_url = os.getenv("RAILWAY_SERVER_URL").rstrip("/")

        meeting_url = args.meeting_url or os.getenv("GOOGLE_MEET_URL")
        if not meeting_url:
            logger.error("--meeting-url required for recall_ai mode (e.g. https://meet.google.com/abc-defg-hij)")
            sys.exit(1)

        SESSION_KEY = "max"
        avatar_page_url = f"{railway_url}/?bot_id={SESSION_KEY}"
        webhook_url = f"{railway_url}/webhook/recall"

        logger.info(f"🌐 Railway brain server : {railway_url}")
        logger.info(f"🎥 Avatar page URL      : {avatar_page_url}")
        logger.info(f"🔗 Webhook URL          : {webhook_url}")

        from max.recall_ai import create_recall_bot, wait_for_bot_to_join, stop_bot

        result = await create_recall_bot(
            meeting_url=meeting_url,
            bot_name=args.bot_name,
            avatar_page_url=avatar_page_url,
            webhook_url=webhook_url,
        )
        bot_id = result.get("id")
        logger.info(f"🤖 Bot ID: {bot_id}")

        final_status = await wait_for_bot_to_join(bot_id)

        if final_status in ("in_call_not_recording", "in_call_recording"):
            logger.info("✅ Max is LIVE in the meeting!")
            logger.info("   Avatar visible + listening via Deepgram transcription")
            logger.info("   Say 'Max' or ask him a question — he will respond")
            logger.info("   Press Ctrl+C to remove Max from the meeting")
            try:
                while True:
                    await asyncio.sleep(10)
            except KeyboardInterrupt:
                logger.info("🛑 Removing Max from the meeting...")
                await stop_bot(bot_id)
        else:
            logger.error(f"❌ Max failed to join (status: {final_status})")

        return

    # --- Get meeting room (Daily / MeetingBaaS modes) ---
    logger.info(f"🔗 Setting up meeting connection (mode: {args.mode})...")
    from max.meeting_join import get_meeting_room
    room = await get_meeting_room(
        mode=args.mode,
        meeting_url=args.meeting_url,
    )

    room_url = room.get("url") or room.get("room_url")
    token = room.get("token")

    if not room_url:
        logger.error("Could not get a meeting room URL")
        sys.exit(1)

    logger.info(f"🏠 Meeting room: {room_url}")

    if args.mode == "daily":
        logger.info(f"👥 Share this link with your team to join: {room_url}")

    # --- Launch Max ---
    logger.info("🤖 Launching Max...")

    if args.free:
        from max.bot_free import create_max_bot_free
        notes = await create_max_bot_free(
            room_url=room_url,
            token=token,
            bot_name=args.bot_name,
            jira_context=jira_context,
        )
    else:
        from max.bot import create_max_bot
        notes = await create_max_bot(
            room_url=room_url,
            token=token,
            bot_name=args.bot_name,
            jira_context=jira_context,
        )

    if notes:
        logger.info("📝 Meeting notes generated")
        print("\n" + "=" * 60)
        print(notes)
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
