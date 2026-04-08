"""
Meeting Joiner — connects Max to Google Meet, Zoom, or Teams via Meeting BaaS.

Meeting BaaS provides a simple API to deploy a speaking bot into any meeting platform.
This module handles creating the bot and managing the WebSocket audio connection.

Two modes supported:
1. Daily.co (default) — Max joins a Daily room. Simplest for testing.
2. Meeting BaaS — Max joins Google Meet / Zoom / Teams as a real participant.
"""

import os
import json
from typing import Optional

import httpx
from loguru import logger


MEETING_BAAS_API_URL = "https://api.meetingbaas.com/v2"


async def create_daily_room() -> dict:
    """Create a temporary Daily.co room for Max to join.

    Returns:
        dict with 'url' and 'token' keys
    """
    api_key = os.getenv("DAILY_API_KEY")
    if not api_key:
        raise ValueError("DAILY_API_KEY not set. Get one from dashboard.daily.co")

    async with httpx.AsyncClient() as client:
        # Create a new room
        response = await client.post(
            "https://api.daily.co/v1/rooms",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "properties": {
                    "exp": None,  # No expiry for standup
                    "enable_chat": True,
                    "enable_screenshare": True,
                    "start_audio_off": False,
                    "start_video_off": True,  # Max doesn't need video initially
                },
            },
            timeout=10,
        )
        response.raise_for_status()
        room = response.json()
        room_url = room["url"]
        room_name = room["name"]

        logger.info(f"✅ Created Daily room: {room_url}")

        # Create a meeting token for Max
        token_response = await client.post(
            "https://api.daily.co/v1/meeting-tokens",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "properties": {
                    "room_name": room_name,
                    "user_name": "Max",
                    "is_owner": False,
                    "enable_screenshare": False,
                    "start_audio_off": False,
                    "start_video_off": True,
                },
            },
            timeout=10,
        )
        token_response.raise_for_status()
        token = token_response.json()["token"]

        return {"url": room_url, "token": token}


async def join_via_meeting_baas(
    meeting_url: str,
    bot_name: str = "Max",
    persona_prompt: str = None,
) -> dict:
    """Deploy Max into a Google Meet / Zoom / Teams meeting via Meeting BaaS.

    This creates a speaking bot that joins the actual meeting platform
    as a participant, with real-time audio I/O via WebSocket.

    Args:
        meeting_url: The Google Meet / Zoom / Teams URL
        bot_name: Display name for Max in the meeting
        persona_prompt: System prompt for Max's personality

    Returns:
        dict with bot_id and websocket connection details
    """
    api_key = os.getenv("MEETING_BAAS_API_KEY")
    if not api_key:
        raise ValueError(
            "MEETING_BAAS_API_KEY not set. Get one from meetingbaas.com"
        )

    async with httpx.AsyncClient() as client:
        payload = {
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "bot_image": None,  # Can add Max's avatar URL here
            "entry_message": None,  # Max enters silently
            "reserved": False,
            "speech_to_text": {
                "provider": "Default",
            },
            "recording": False,  # Set True if you want meeting recordings
        }

        # If using the speaking bot API endpoint
        speaking_payload = {
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "bot_image": None,
        }

        response = await client.post(
            f"{MEETING_BAAS_API_URL}/bots/speaking",
            headers={
                "x-spoke-api-key": api_key,
                "Content-Type": "application/json",
            },
            json=speaking_payload,
            timeout=30,
        )

        if response.status_code == 200:
            result = response.json()
            logger.info(f"✅ Max is joining {meeting_url} via Meeting BaaS")
            logger.info(f"   Bot ID: {result.get('bot_id')}")
            return result
        else:
            logger.error(f"Meeting BaaS error: {response.status_code} — {response.text}")
            raise RuntimeError(f"Failed to join meeting: {response.text}")


async def get_meeting_room(mode: str = "daily", meeting_url: str = None) -> dict:
    """Get or create a meeting room for Max.

    Args:
        mode: "daily" for Daily.co rooms, "meeting_baas" for Google Meet/Zoom/Teams
        meeting_url: Required for meeting_baas mode — the actual meeting link

    Returns:
        dict with connection details
    """
    if mode == "daily":
        # Check if a room URL was provided
        room_url = os.getenv("DAILY_ROOM_URL")
        if room_url:
            return {"url": room_url, "token": None}
        # Create a new room
        return await create_daily_room()

    elif mode == "meeting_baas":
        if not meeting_url:
            meeting_url = os.getenv("GOOGLE_MEET_URL")
        if not meeting_url:
            raise ValueError("Meeting URL required for meeting_baas mode")
        return await join_via_meeting_baas(meeting_url)

    else:
        raise ValueError(f"Unknown mode: {mode}. Use 'daily' or 'meeting_baas'")
