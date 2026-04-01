#!/bin/bash
# ============================================================
# start_max.sh — Launch Max into a Google Meet via Meeting BaaS
#
# Usage:
#   ./start_max.sh                         # joins test meeting
#   ./start_max.sh --meeting-url "https://meet.google.com/xxx-xxxx-xxx"
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Activate virtual environment
if [ ! -d "venv" ]; then
  echo "❌  venv not found. Run setup first:"
  echo "    python3.11 -m venv venv"
  echo "    source venv/bin/activate"
  echo "    pip install pipecat-ai[anthropic,deepgram,daily,silero] httpx python-dotenv loguru aiohttp"
  exit 1
fi

source venv/bin/activate

MEET_URL=$(grep GOOGLE_MEET_URL .env | cut -d= -f2)

echo ""
echo "🤖  Max AI Employee — Audio Only (Meeting BaaS)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔗  Meet  : ${MEET_URL}"
echo "🧠  Brain : Claude (Anthropic)"
echo "👂  Ears  : Deepgram STT"
echo "🗣️  Voice : Deepgram Aura TTS"
echo ""

python3 run.py --mode meeting_baas "$@"
