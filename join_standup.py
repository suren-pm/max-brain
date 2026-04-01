#!/usr/bin/env python3
"""
join_standup.py — Send Max to a Google Meet via Recall.ai.

Usage:
    python3 join_standup.py
    python3 join_standup.py --meeting-url "https://meet.google.com/mmg-mjgn-njd"

What it does:
    1. Creates a Recall.ai bot that joins the Google Meet with avatar + transcription
    2. Polls until the bot reaches a live state (waiting room or in-call) — success
    3. Exits — the bot stays in the meeting independently via the Railway brain server

Success states  : in_waiting_room, in_call_not_recording, in_call_recording
Failure states  : fatal, error, call_ended, done
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
TEST_MEETING   = "https://meet.google.com/mmg-mjgn-njd"   # ← ALWAYS use this for testing
RAILWAY_URL    = os.getenv("RAILWAY_SERVER_URL", "https://max-brain-production.up.railway.app").rstrip("/")
SUCCESS_STATES = {"in_waiting_room", "in_call_not_recording", "in_call_recording"}
FATAL_STATES   = {"fatal", "error", "call_ended", "done"}
# ──────────────────────────────────────────────────────────────────────────────


async def main() -> None:
    parser = argparse.ArgumentParser(description="Send Max to a Google Meet standup")
    parser.add_argument(
        "--meeting-url",
        default=TEST_MEETING,
        help=f"Google Meet URL to join (default: {TEST_MEETING})",
    )
    args = parser.parse_args()

    meeting_url    = args.meeting_url
    avatar_page_url = f"{RAILWAY_URL}/?bot_id=max"
    webhook_url    = f"{RAILWAY_URL}/webhook/recall"

    print(f"🤖 Meeting  : {meeting_url}")
    print(f"🧠 Brain    : {RAILWAY_URL}")
    print(f"🎥 Avatar   : {avatar_page_url}")
    print(f"🔗 Webhook  : {webhook_url}")
    print()

    # ── Import brain modules ──────────────────────────────────────────────────
    from max.recall_ai import create_recall_bot, get_bot_status

    # ── Create bot ────────────────────────────────────────────────────────────
    result = await create_recall_bot(
        meeting_url=meeting_url,
        bot_name="Max",
        avatar_page_url=avatar_page_url,
        webhook_url=webhook_url,
    )
    bot_id: str = result["id"]
    print(f"✅ Bot created — ID: {bot_id}")

    # ── Poll until live or failed (45 s max) ──────────────────────────────────
    for attempt in range(15):          # 15 × 3 s = 45 s
        await asyncio.sleep(3)
        data    = await get_bot_status(bot_id)
        changes = data.get("status_changes", [])
        status  = changes[-1]["code"] if changes else "unknown"
        print(f"   {attempt + 1:2}/15  status: {status}")

        if status in SUCCESS_STATES:
            print(f"\n✅ Max is live — status: {status}")
            if status == "in_waiting_room":
                print("   (Waiting room — host will admit Max when standup starts)")
            print(f"   Bot ID : {bot_id}")
            print("   Your Mac can now sleep. Brain keeps running on Railway.")
            return

        if status in FATAL_STATES:
            print(f"\n❌ Bot failed — status: {status}", file=sys.stderr)
            sys.exit(1)

    # Still joining after 45 s — bot is alive, just slow
    print("\n⚠️  Still 'joining' after 45 s — bot is alive, not yet admitted.")
    print(f"   Bot ID : {bot_id}")
    print("   Check the Recall.ai dashboard if needed.")


if __name__ == "__main__":
    asyncio.run(main())
