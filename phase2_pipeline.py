#!/usr/bin/env python3
"""
Max Phase 2 — Local AI Avatar Pipeline
=======================================
Fully free, local stack — no Recall.ai credits needed.

Architecture:
  OBS Virtual Camera (Max's face) → Google Meet camera input
  BlackHole (TTS audio)           → Google Meet mic input
  Deepgram STT                    → Transcribes meeting audio
  Claude (local/Railway)          → Generates responses
  Deepgram/pyttsx3 TTS            → Synthesizes speech
  MuseTalk                        → Animates Max's face with lip sync

Usage:
    cd ~/max-ai-employee
    source venv/bin/activate     # if using project venv
    python3 phase2_pipeline.py --meeting-url "https://meet.google.com/mmg-mjgn-njd"
    python3 phase2_pipeline.py --test   # test MuseTalk animation only

Requirements (one-time Mac setup):
    1. brew install --cask blackhole-2ch
    2. OBS installed at /Applications/OBS.app (already installed ✅)
    3. OBS Virtual Camera enabled in OBS
    4. pip install playwright && playwright install chromium
    5. pip install deepgram-sdk anthropic httpx loguru python-dotenv
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import os
import subprocess
import sys
import threading
import time
import queue
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────────
PROJECT_ROOT  = Path(__file__).parent
MUSETALK_DIR  = Path.home() / "MuseTalk"
FACE_IMAGE    = PROJECT_ROOT / "max_face.jpg"
RAILWAY_URL   = os.getenv("RAILWAY_SERVER_URL", "https://max-brain-production.up.railway.app").rstrip("/")
TEST_MEETING  = "https://meet.google.com/mmg-mjgn-njd"

# ── Audio devices (Mac) ────────────────────────────────────────────────────
BLACKHOLE_MIC  = "BlackHole 2ch"   # Virtual mic → Google Meet sees this as Max's mic
SYSTEM_OUTPUT  = "MacBook Air Speakers"  # Or "External Headphones" for headset

# ── Speech queue ───────────────────────────────────────────────────────────
speech_queue: queue.Queue = queue.Queue()
musetalk_frame_queue: queue.Queue = queue.Queue(maxsize=100)


# ═══════════════════════════════════════════════════════════════════════════
# 1. MuseTalk Animation Engine
# ═══════════════════════════════════════════════════════════════════════════

class MuseTalkEngine:
    """Drives MuseTalk to animate Max's face from audio."""

    def __init__(self):
        self.musetalk_dir = MUSETALK_DIR
        self.face_image   = FACE_IMAGE
        self.venv_python  = self.musetalk_dir / "venv" / "bin" / "python3"
        self.ready        = False

    def check_ready(self) -> bool:
        required = [
            self.musetalk_dir / "models" / "musetalk" / "pytorch_model.bin",
            self.musetalk_dir / "models" / "musetalkV15" / "unet.pth",
            self.musetalk_dir / "models" / "whisper" / "pytorch_model.bin",
            self.face_image,
        ]
        missing = [p for p in required if not p.exists()]
        if missing:
            logger.warning(f"MuseTalk missing files: {[str(p) for p in missing]}")
            return False
        if not self.venv_python.exists():
            logger.warning(f"MuseTalk venv not found at {self.venv_python}")
            return False
        self.ready = True
        logger.info("✅ MuseTalk engine ready")
        return True

    def generate_video(self, audio_path: Path, output_path: Path) -> bool:
        """Run MuseTalk to generate a lip-synced video from audio."""
        if not self.ready:
            logger.warning("MuseTalk not ready — skipping animation")
            return False
        cmd = [
            str(self.venv_python), "-m", "scripts.inference",
            "--avatar_path", str(self.face_image),
            "--audio_path",  str(audio_path),
            "--result_dir",  str(output_path.parent),
            "--use_float16",
        ]
        logger.info(f"🎬 Running MuseTalk: {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.musetalk_dir),
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                logger.info(f"✅ MuseTalk video generated: {output_path}")
                return True
            else:
                logger.error(f"MuseTalk failed: {result.stderr[-500:]}")
                return False
        except subprocess.TimeoutExpired:
            logger.error("MuseTalk timed out")
            return False
        except Exception as e:
            logger.error(f"MuseTalk error: {e}")
            return False

    def play_video_to_obs(self, video_path: Path):
        """Send video to OBS via its websocket or local file source."""
        # OBS watches a temp file path — we replace the file atomically
        obs_input_path = Path("/tmp/max_speaking.mp4")
        if video_path.exists():
            import shutil
            shutil.copy2(str(video_path), str(obs_input_path))
            logger.info(f"📺 Video sent to OBS input: {obs_input_path}")


# ═══════════════════════════════════════════════════════════════════════════
# 2. Text-to-Speech (Deepgram Aura)
# ═══════════════════════════════════════════════════════════════════════════

async def generate_tts(text: str, output_path: Path) -> bool:
    """Generate speech MP3 using Deepgram Aura TTS."""
    import httpx
    key = os.getenv("DEEPGRAM_API_KEY")
    if not key:
        logger.warning("DEEPGRAM_API_KEY not set — using fallback TTS")
        return await fallback_tts(text, output_path)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.deepgram.com/v1/speak?model=aura-arcas-en",
                headers={"Authorization": f"Token {key}", "Content-Type": "application/json"},
                json={"text": text},
                timeout=15,
            )
        if resp.status_code == 200:
            output_path.write_bytes(resp.content)
            logger.info(f"🔊 TTS generated: {output_path} ({len(resp.content)/1024:.0f} KB)")
            return True
        logger.warning(f"Deepgram TTS {resp.status_code}: {resp.text[:80]}")
    except Exception as e:
        logger.error(f"TTS error: {e}")
    return await fallback_tts(text, output_path)


async def fallback_tts(text: str, output_path: Path) -> bool:
    """Fallback: system TTS via say command (macOS)."""
    try:
        # Use 'say' to generate AIFF then convert to MP3 with ffmpeg
        aiff_path = output_path.with_suffix(".aiff")
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", "Daniel", "-o", str(aiff_path), text
        )
        await proc.wait()
        # Convert to MP3
        proc2 = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(aiff_path), str(output_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc2.wait()
        aiff_path.unlink(missing_ok=True)
        logger.info(f"🔊 Fallback TTS (say -v Daniel): {output_path}")
        return output_path.exists()
    except Exception as e:
        logger.error(f"Fallback TTS error: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
# 3. Audio Routing via BlackHole
# ═══════════════════════════════════════════════════════════════════════════

async def play_audio_via_blackhole(audio_path: Path):
    """Play audio file routed through BlackHole → Google Meet sees it as mic input."""
    # ffplay → BlackHole device
    try:
        cmd = [
            "ffplay",
            "-nodisp",
            "-autoexit",
            "-audio_device_name", BLACKHOLE_MIC,
            str(audio_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        logger.info("✅ Audio played via BlackHole")
    except Exception as e:
        logger.warning(f"BlackHole playback failed ({e}) — trying afplay fallback")
        # Fallback: afplay (plays through default output — works for testing)
        proc = await asyncio.create_subprocess_exec(
            "afplay", str(audio_path)
        )
        await proc.wait()


# ═══════════════════════════════════════════════════════════════════════════
# 4. Google Meet Chrome Automation (Playwright)
# ═══════════════════════════════════════════════════════════════════════════

async def join_google_meet(meeting_url: str, bot_name: str = "Max") -> bool:
    """
    Open Google Meet in Chrome using Playwright.
    Uses OBS Virtual Camera as the camera and BlackHole as the microphone.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("playwright not installed — run: pip install playwright && playwright install chromium")
        return False

    logger.info(f"🌐 Joining Google Meet: {meeting_url}")
    logger.info(f"   Camera: OBS Virtual Camera")
    logger.info(f"   Mic: {BLACKHOLE_MIC}")

    # Chrome args to use specific audio/video devices
    chrome_args = [
        "--use-fake-device-for-media-stream",      # allow fake media
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        # Grant all permissions without prompt
        "--allow-running-insecure-content",
        "--unsafely-treat-insecure-origin-as-secure",
        # Autoplay policy
        "--autoplay-policy=no-user-gesture-required",
    ]

    async with async_playwright() as p:
        # Launch Chromium with camera/mic permissions
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=str(Path.home() / ".max_chrome_profile"),
            headless=False,
            args=chrome_args,
            permissions=["camera", "microphone"],
            channel="chrome",      # use real Chrome (not bundled Chromium) for Meet compatibility
        )

        page = browser.pages[0] if browser.pages else await browser.new_page()

        # Grant camera/mic permissions for Google Meet domain
        await browser.grant_permissions(["camera", "microphone"], origin="https://meet.google.com")

        logger.info("🌐 Navigating to Google Meet...")
        await page.goto(meeting_url, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # Handle "join before host" or name entry screen
        try:
            name_input = page.locator('input[placeholder*="name" i], input[aria-label*="name" i]')
            await name_input.wait_for(timeout=5000)
            await name_input.fill(bot_name)
            logger.info(f"✏️  Entered name: {bot_name}")
        except Exception:
            logger.info("No name input found (may already be signed in)")

        # Dismiss camera/mic dialogs
        await asyncio.sleep(2)

        # Turn off mic and camera before joining (we control them ourselves)
        # Look for the mute mic button
        try:
            mic_btn = page.locator('[data-priv="mgr-btn-join-mute-mic"], [aria-label*="microphone" i]').first
            await mic_btn.wait_for(timeout=3000)
            # We'll leave mic ON so BlackHole audio comes through
            logger.info("🎤 Microphone control found")
        except Exception:
            pass

        # Click "Join now" / "Ask to join"
        join_selectors = [
            'button:has-text("Join now")',
            'button:has-text("Ask to join")',
            'button:has-text("Join")',
            '[data-priv="mgr-btn-join-join"]',
            'div[jsname="Qx7uuf"]',
        ]
        joined = False
        for sel in join_selectors:
            try:
                btn = page.locator(sel).first
                await btn.wait_for(timeout=3000)
                await btn.click()
                logger.info(f"✅ Clicked join button: {sel}")
                joined = True
                break
            except Exception:
                continue

        if not joined:
            logger.warning("⚠️  Could not find join button — may already be in meeting")

        await asyncio.sleep(3)
        logger.info("🎉 Max is in the Google Meet!")
        logger.info("   Say 'Max' to trigger a response")
        logger.info("   Press Ctrl+C to leave the meeting")

        # Keep alive — poll Railway brain for speech responses
        try:
            while True:
                await poll_and_speak()
                await asyncio.sleep(0.5)
        except KeyboardInterrupt:
            logger.info("🛑 Leaving meeting...")

        await browser.close()
        return True


# ═══════════════════════════════════════════════════════════════════════════
# 5. Poll Railway Brain for Responses
# ═══════════════════════════════════════════════════════════════════════════

musetalk_engine: MuseTalkEngine = None


async def poll_and_speak():
    """Poll Railway brain for queued speech, generate animation, play it."""
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{RAILWAY_URL}/speech/max", timeout=3)
            data = resp.json()
    except Exception:
        return

    if not data.get("text"):
        return

    text  = data["text"]
    audio_b64 = data.get("audio")

    logger.info(f"🗣️  Max says: {text[:60]}...")

    audio_path = Path("/tmp/max_speech.mp3")

    if audio_b64:
        # Use server-generated Deepgram audio
        audio_path.write_bytes(base64.b64decode(audio_b64))
    else:
        # Generate TTS locally
        await generate_tts(text, audio_path)

    if not audio_path.exists():
        logger.error("No audio generated")
        return

    # Animate face with MuseTalk
    if musetalk_engine and musetalk_engine.ready:
        video_path = Path("/tmp/max_speaking.mp4")
        if musetalk_engine.generate_video(audio_path, video_path):
            musetalk_engine.play_video_to_obs(video_path)

    # Play audio via BlackHole → Google Meet
    await play_audio_via_blackhole(audio_path)


# ═══════════════════════════════════════════════════════════════════════════
# 6. MuseTalk Quick Test
# ═══════════════════════════════════════════════════════════════════════════

async def test_musetalk():
    """Standalone test: generate a short animated video of Max speaking."""
    logger.info("🧪 Testing MuseTalk animation pipeline...")

    engine = MuseTalkEngine()
    if not engine.check_ready():
        logger.error("MuseTalk is not ready. Check model weights.")
        return False

    # Generate test TTS audio
    test_text = "Hello team! I'm Max, your AI test automation engineer. Ready for standup."
    audio_path = Path("/tmp/max_test.mp3")
    ok = await generate_tts(test_text, audio_path)
    if not ok:
        logger.error("TTS failed")
        return False

    # Run MuseTalk
    video_path = Path("/tmp/max_test_output.mp4")
    ok = engine.generate_video(audio_path, video_path)
    if ok:
        logger.info(f"🎉 Test successful! Video: {video_path}")
        logger.info(f"   Play it with: open {video_path}")
        # Auto-open on Mac
        subprocess.run(["open", str(video_path)], check=False)
        return True
    else:
        logger.error("MuseTalk test failed")
        return False


# ═══════════════════════════════════════════════════════════════════════════
# 7. Main
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    global musetalk_engine

    parser = argparse.ArgumentParser(description="Max Phase 2 — Local AI Avatar")
    parser.add_argument("--meeting-url", default=TEST_MEETING)
    parser.add_argument("--bot-name",    default="Max")
    parser.add_argument("--test",        action="store_true", help="Test MuseTalk animation only")
    parser.add_argument("--tts-test",    action="store_true", help="Test TTS + BlackHole audio only")
    args = parser.parse_args()

    logger.info("🤖 Max Phase 2 — Local AI Avatar Pipeline")
    logger.info(f"   Meeting  : {args.meeting_url}")
    logger.info(f"   Brain    : {RAILWAY_URL}")
    logger.info(f"   MuseTalk : {MUSETALK_DIR}")
    logger.info(f"   Face     : {FACE_IMAGE}")

    # Init MuseTalk
    musetalk_engine = MuseTalkEngine()
    musetalk_engine.check_ready()

    if args.test:
        await test_musetalk()
        return

    if args.tts_test:
        text = "Hey team! I'm Max. Testing audio routing through BlackHole. Can you hear me?"
        audio_path = Path("/tmp/max_tts_test.mp3")
        ok = await generate_tts(text, audio_path)
        if ok:
            logger.info("🔊 Playing audio via BlackHole...")
            await play_audio_via_blackhole(audio_path)
            logger.info("✅ TTS test complete")
        return

    # Join the meeting
    await join_google_meet(args.meeting_url, args.bot_name)


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | <level>{message}</level>")
    asyncio.run(main())
