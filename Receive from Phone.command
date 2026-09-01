#!/bin/bash
# Double-click this to start receiving files FROM your phone onto this Mac.
# Leave the window open; press Ctrl-C (or close it) to stop.
# Your phone's LocalSend app will see this Mac as a device called "MacBook".
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Starting receiver. Your phone will see this Mac as 'MacBook' in LocalSend."
echo "Files land in your save folder (default: ~/Drawbridge)."
echo "Leave this window open. Press Ctrl-C to stop."
echo
exec python3 "$DIR/drawbridge/receive_on_mac.py"
