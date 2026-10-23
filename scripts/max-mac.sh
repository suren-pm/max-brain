#!/bin/bash
# Start, stop or check Max on the Mac (self-hosted, 2026-10-23).
#
#   scripts/max-mac.sh start     start max-brain (8090) and max-bot (8091)
#   scripts/max-mac.sh stop      leave any meeting, then stop both
#   scripts/max-mac.sh restart
#   scripts/max-mac.sh status    health of both, and whether Max is in a meeting
#   scripts/max-mac.sh logs      follow both logs
#
# Nothing runs at login; you start Max yourself (Suren's choice, 23 Oct 2026).
# Logs: ~/Library/Logs/max/ (the last 5 runs are kept as .1 to .5).

BRAIN_DIR="${MAX_BRAIN_DIR:-$HOME/Documents/Claude/Max AI Employee}"
BOT_DIR="${MAX_BOT_DIR:-$HOME/Documents/Claude/max-bot}"
LOG_DIR="$HOME/Library/Logs/max"
BRAIN_PORT=8090
BOT_PORT=8091

pid_on() { lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -1; }

rotate() {
    local f="$LOG_DIR/$1.log"
    [ -f "$f" ] || return 0
    for i in 4 3 2 1; do [ -f "$f.$i" ] && mv -f "$f.$i" "$f.$((i + 1))"; done
    mv -f "$f" "$f.1"
}

wait_port() {
    for _ in $(seq 1 30); do [ -n "$(pid_on "$1")" ] && return 0; sleep 1; done
    return 1
}

start_brain() {
    if [ -n "$(pid_on $BRAIN_PORT)" ]; then echo "max-brain already running (port $BRAIN_PORT)"; return 0; fi
    rotate max-brain
    cd "$BRAIN_DIR" || { echo "max-brain folder not found: $BRAIN_DIR"; return 1; }
    # PORT must match --port: the brain's internal pipeline socket reads it.
    # max-brain doesn't load .env by itself, hence --env-file.
    PORT=$BRAIN_PORT nohup .venv311/bin/python -m uvicorn max.server:app \
        --host 127.0.0.1 --port $BRAIN_PORT --env-file .env \
        >> "$LOG_DIR/max-brain.log" 2>&1 &
    wait_port $BRAIN_PORT && echo "max-brain started (port $BRAIN_PORT)" \
        || { echo "max-brain did not start; see $LOG_DIR/max-brain.log"; return 1; }
}

start_bot() {
    if [ -n "$(pid_on $BOT_PORT)" ]; then echo "max-bot already running (port $BOT_PORT)"; return 0; fi
    rotate max-bot
    cd "$BOT_DIR" || { echo "max-bot folder not found: $BOT_DIR"; return 1; }
    export NVM_DIR="$HOME/.nvm"
    . "$NVM_DIR/nvm.sh" && nvm use 20 > /dev/null || { echo "Node 20 (nvm) not available"; return 1; }
    unset NODE_ENV
    MAX_BRAIN_WS_URL="ws://localhost:$BRAIN_PORT/ws" PORT=$BOT_PORT \
        nohup node build/src/app.js >> "$LOG_DIR/max-bot.log" 2>&1 &
    wait_port $BOT_PORT && echo "max-bot started (port $BOT_PORT)" \
        || { echo "max-bot did not start; see $LOG_DIR/max-bot.log"; return 1; }
}

leave_meetings() {
    local ids
    ids=$(curl -s --max-time 5 "http://127.0.0.1:$BOT_PORT/diag/sessions" \
        | python3 -c 'import json,sys; [print(s["bot_id"]) for s in json.load(sys.stdin).get("sessions", [])]' 2>/dev/null)
    for id in $ids; do
        curl -s --max-time 20 -X POST "http://127.0.0.1:$BOT_PORT/leave/$id" > /dev/null && echo "Max left the meeting ($id)"
    done
}

stop_port() {
    local name=$1 port=$2 pid
    pid=$(pid_on "$port")
    if [ -z "$pid" ]; then echo "$name not running"; return 0; fi
    kill "$pid"
    for _ in $(seq 1 10); do [ -z "$(pid_on "$port")" ] && { echo "$name stopped"; return 0; }; sleep 1; done
    echo "$name (pid $pid) did not stop within 10 s"; return 1
}

status() {
    local h
    h=$(curl -s --max-time 8 "http://127.0.0.1:$BRAIN_PORT/health")
    if [ -z "$h" ]; then echo "max-brain: not running"; else echo "max-brain: $h"; fi
    if [ -n "$(pid_on $BOT_PORT)" ]; then
        echo "max-bot: running, sessions: $(curl -s --max-time 5 "http://127.0.0.1:$BOT_PORT/diag/sessions")"
    else
        echo "max-bot: not running"
    fi
}

mkdir -p "$LOG_DIR"
case "${1:-status}" in
    start)   start_brain && start_bot && status ;;
    stop)    leave_meetings; stop_port max-bot $BOT_PORT; stop_port max-brain $BRAIN_PORT ;;
    restart) "$0" stop; "$0" start ;;
    status)  status ;;
    logs)    tail -n 50 -f "$LOG_DIR/max-brain.log" "$LOG_DIR/max-bot.log" ;;
    *)       echo "usage: $0 start|stop|restart|status|logs"; exit 2 ;;
esac
