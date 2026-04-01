"""
Recall.ai integration — deploys Max into Google Meet with live video avatar + transcription.
"""
from __future__ import annotations

import asyncio
import os

import httpx
from loguru import logger

RECALL_API_BASE = "https://ap-northeast-1.recall.ai/api/v1"


async def create_recall_bot(
    meeting_url: str,
    bot_name: str = "Max",
    avatar_page_url: str = None,
    webhook_url: str = None,
) -> dict:
    api_key = os.getenv("RECALL_AI_API_KEY")
    if not api_key:
        raise ValueError("RECALL_AI_API_KEY not set in .env")

    deepgram_key = os.getenv("DEEPGRAM_API_KEY")

    payload: dict = {
        "meeting_url": meeting_url,
        "bot_name": bot_name,
    }

    # Webhook receives all bot events (status changes + transcript notifications)
    if webhook_url:
        payload["webhook_url"] = webhook_url
        logger.info(f"🎙️  Webhook: {webhook_url}")

    # Enable real-time transcription via Deepgram so Max can hear the meeting
    if deepgram_key:
        realtime_endpoints = []
        if webhook_url:
            # CRITICAL: transcripts go to realtime_endpoints, NOT the top-level webhook_url
            realtime_endpoints = [
                {
                    "type": "webhook",
                    "url": webhook_url,
                    "events": ["transcript.data"],
                }
            ]
            logger.info(f"📡  Realtime transcript endpoint: {webhook_url}")

        payload["recording_config"] = {
            "transcript": {
                "provider": {
                    "deepgram_streaming": {
                        "model": "nova-2-conversationalai",
                        "language": "en",
                    }
                }
            },
            "realtime_endpoints": realtime_endpoints,
        }
        logger.info("🎙️  Deepgram transcription enabled")
    else:
        logger.warning("DEEPGRAM_API_KEY not set — Max will join without transcription (can't hear)")

    # Avatar page rendered as Max's camera feed
    if avatar_page_url:
        payload["output_media"] = {
            "camera": {
                "kind": "webpage",
                "config": {"url": avatar_page_url},
            }
        }
        logger.info(f"🎥  Avatar page: {avatar_page_url}")
    else:
        logger.warning("No avatar_page_url — Max joins without a video face")

    async with httpx.AsyncClient() as client:
        logger.info(f"🤖  Creating Recall.ai bot for: {meeting_url}")
        resp = await client.post(
            f"{RECALL_API_BASE}/bot/",
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )

    if resp.status_code in (200, 201):
        result = resp.json()
        logger.info(f"✅  Bot created — ID: {result.get('id')}")
        logger.info(f"    '{bot_name}' is joining the meeting...")
        return result

    logger.error(f"❌  Recall.ai error {resp.status_code}: {resp.text}")
    raise RuntimeError(f"Failed to create bot: {resp.status_code} — {resp.text}")


async def get_bot_status(bot_id: str) -> dict:
    api_key = os.getenv("RECALL_AI_API_KEY")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{RECALL_API_BASE}/bot/{bot_id}/",
            headers={"Authorization": f"Token {api_key}"},
            timeout=10,
        )
    resp.raise_for_status()
    data = resp.json()
    changes = data.get("status_changes", [])
    latest = changes[-1]["code"] if changes else "unknown"
    logger.info(f"   Bot {bot_id[:8]}... status: {latest}")
    return data


async def stop_bot(bot_id: str) -> bool:
    api_key = os.getenv("RECALL_AI_API_KEY")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{RECALL_API_BASE}/bot/{bot_id}/leave_call/",
            headers={"Authorization": f"Token {api_key}"},
            timeout=10,
        )
    success = resp.status_code in (200, 204)
    if success:
        logger.info(f"✅  Bot {bot_id[:8]}... left the meeting")
    else:
        logger.warning(f"Could not remove bot: {resp.status_code}")
    return success


async def wait_for_bot_to_join(bot_id: str, timeout_seconds: int = 60) -> str:
    logger.info(f"⏳  Waiting for bot to join (up to {timeout_seconds}s)...")
    elapsed = 0
    while elapsed < timeout_seconds:
        data = await get_bot_status(bot_id)
        changes = data.get("status_changes", [])
        latest = changes[-1]["code"] if changes else "unknown"
        if latest in ("in_call_not_recording", "in_call_recording"):
            logger.info(f"🎉  Max is in the meeting! (status: {latest})")
            return latest
        if latest in ("fatal", "error", "call_ended"):
            logger.error(f"❌  Bot failed to join: {latest}")
            return latest
        await asyncio.sleep(3)
        elapsed += 3
    logger.warning("⚠️   Timed out waiting for bot to join")
    return "timeout"
