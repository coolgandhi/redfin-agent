#!/bin/bash
# Wrapper for launchd: skip if agent already ran today (handles missed wake-up retries)
LOG="/Users/sachin/Documents/code/redfin_agent/agent.log"
AGENT="/Users/sachin/Documents/code/redfin_agent/redfin_agent.py"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"

if [ -f "$LOG" ]; then
    last_run=$(stat -f "%Sm" -t "%Y-%m-%d" "$LOG")
    today=$(date "+%Y-%m-%d")
    [ "$last_run" = "$today" ] && exit 0
fi

exec "$PYTHON" "$AGENT"
