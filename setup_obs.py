#!/usr/bin/env python3
"""
OBS Setup Helper for Max Phase 2
=================================
Automates OBS configuration so Max's animated face appears as the virtual camera.

Run once to configure OBS:
    python3 setup_obs.py

What this sets up:
    - OBS scene "Max" with:
      1. Media Source: /tmp/max_speaking.mp4 (MuseTalk output — loops while Max speaks)
      2. Image Source: max_face.jpg (fallback idle face)
      3. Browser Source: max_avatar.html (status overlay)
    - Virtual Camera output → Google Meet sees Max's animated face

Requirements:
    pip install obs-websocket-py
    OBS must be running with WebSocket server enabled (Tools → WebSocket Server Settings)
    Default: localhost:4455, no password
"""
import sys
import os
from pathlib import Path

try:
    from obswebsocket import obsws, requests as obsreq
    OBS_WS_AVAILABLE = True
except ImportError:
    OBS_WS_AVAILABLE = False

PROJECT_ROOT = Path(__file__).parent
FACE_IMAGE   = PROJECT_ROOT / "max_face.jpg"
AVATAR_HTML  = PROJECT_ROOT / "max_avatar.html"
RAILWAY_URL  = os.getenv("RAILWAY_SERVER_URL", "https://max-brain-production.up.railway.app")

OBS_HOST     = "localhost"
OBS_PORT     = 4455
OBS_PASSWORD = ""   # Set if you configured a password in OBS WebSocket settings


def print_manual_instructions():
    """Print step-by-step OBS setup instructions."""
    print("""
╔══════════════════════════════════════════════════════════════════╗
║              OBS SETUP FOR MAX — STEP BY STEP                   ║
╚══════════════════════════════════════════════════════════════════╝

1. OPEN OBS
   /Applications/OBS.app

2. CREATE A NEW SCENE called "Max"
   - Bottom left → "+" under Scenes → Name it "Max"

3. ADD SOURCES (in this order):

   A. BROWSER SOURCE — Max Avatar Overlay
      → Click "+" under Sources → "Browser"
      → Name: "Max Avatar"
      → URL: file:///{avatar_html}
         (or the Railway URL: {railway_url}/?bot_id=max)
      → Width: 1280, Height: 720
      → ✅ Control audio via OBS

   B. IMAGE SOURCE — Max's Face (idle)
      → Click "+" → "Image"
      → Name: "Max Face Idle"
      → Image File: {face_image}
      → Scale to fit the scene

   C. MEDIA SOURCE — MuseTalk Speaking Video (auto-updates)
      → Click "+" → "Media Source"
      → Name: "Max Speaking"
      → ✅ Local File
      → File path: /tmp/max_speaking.mp4
      → ✅ Loop
      → ✅ Restart playback when source becomes active

4. LAYER ORDER (top to bottom in Sources panel):
      Max Avatar (browser overlay — top)
      Max Speaking (video — middle)
      Max Face Idle (image — bottom/background)

5. SCENE CANVAS
   → Settings → Video → Base Resolution: 1280×720
   → Output Resolution: 1280×720

6. VIRTUAL CAMERA
   → Click "Start Virtual Camera" button (bottom right)
   → This makes OBS appear as "OBS Virtual Camera" in Google Meet

7. AUDIO SETUP
   → In OBS: Settings → Audio
   → Desktop Audio: BlackHole 2ch  (captures TTS output)
   → Mic/Aux: Disabled (we don't need OBS to capture the real mic)

   ⚠️  IMPORTANT: In Google Meet settings:
   → Microphone: BlackHole 2ch
   → Camera: OBS Virtual Camera

8. TEST
   → Run: python3 phase2_pipeline.py --tts-test
   → You should see/hear Max speaking in OBS preview

""".format(
        avatar_html=AVATAR_HTML,
        face_image=FACE_IMAGE,
        railway_url=RAILWAY_URL,
    ))


def setup_obs_via_websocket():
    """Auto-configure OBS via WebSocket API."""
    if not OBS_WS_AVAILABLE:
        print("obs-websocket-py not installed.")
        print("Install: pip install obs-websocket-py")
        print_manual_instructions()
        return

    print(f"Connecting to OBS WebSocket at {OBS_HOST}:{OBS_PORT}...")
    try:
        ws = obsws(OBS_HOST, OBS_PORT, OBS_PASSWORD)
        ws.connect()
        print("✅ Connected to OBS!")
    except Exception as e:
        print(f"❌ Could not connect to OBS WebSocket: {e}")
        print("Make sure OBS is running and WebSocket Server is enabled.")
        print("(OBS → Tools → WebSocket Server Settings → Enable)")
        print_manual_instructions()
        return

    try:
        # Create scene
        try:
            ws.call(obsreq.CreateScene(sceneName="Max"))
            print("✅ Created scene 'Max'")
        except Exception:
            print("ℹ️  Scene 'Max' already exists")

        # Switch to Max scene
        ws.call(obsreq.SetCurrentProgramScene(sceneName="Max"))

        # Add Browser Source (Max Avatar)
        try:
            ws.call(obsreq.CreateInput(
                sceneName="Max",
                inputName="Max Avatar",
                inputKind="browser_source",
                inputSettings={
                    "url": f"file://{AVATAR_HTML}",
                    "width": 1280,
                    "height": 720,
                    "css": "",
                    "shutdown": False,
                    "restart_when_active": False,
                },
            ))
            print("✅ Added Browser Source: Max Avatar")
        except Exception as e:
            print(f"ℹ️  Browser Source: {e}")

        # Add Image Source (idle face)
        try:
            ws.call(obsreq.CreateInput(
                sceneName="Max",
                inputName="Max Face Idle",
                inputKind="image_source",
                inputSettings={"file": str(FACE_IMAGE)},
            ))
            print("✅ Added Image Source: Max Face Idle")
        except Exception as e:
            print(f"ℹ️  Image Source: {e}")

        # Add Media Source (MuseTalk output)
        try:
            ws.call(obsreq.CreateInput(
                sceneName="Max",
                inputName="Max Speaking",
                inputKind="ffmpeg_source",
                inputSettings={
                    "local_file": "/tmp/max_speaking.mp4",
                    "looping": True,
                    "restart_on_activate": True,
                    "close_when_inactive": False,
                },
            ))
            print("✅ Added Media Source: Max Speaking")
        except Exception as e:
            print(f"ℹ️  Media Source: {e}")

        print("\n✅ OBS scene configured!")
        print("   → Click 'Start Virtual Camera' in OBS")
        print("   → Select 'OBS Virtual Camera' in Google Meet settings")

    finally:
        ws.disconnect()


if __name__ == "__main__":
    print("🎥 Max OBS Setup Helper")
    print("=" * 50)

    # Check if OBS is running
    import subprocess
    result = subprocess.run(["pgrep", "-x", "OBS"], capture_output=True)
    obs_running = result.returncode == 0

    if not obs_running:
        print("⚠️  OBS is not running. Launch it first:")
        print("   open /Applications/OBS.app")
        print()

    setup_obs_via_websocket()
