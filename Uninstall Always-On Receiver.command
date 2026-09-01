#!/bin/bash
# Double-click to stop and remove the always-on background receiver.
set -euo pipefail
PLIST="$HOME/Library/LaunchAgents/com.drawbridge.receiver.plist"

if [ -f "$PLIST" ]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed. The Mac will no longer receive in the background."
else
    echo "Nothing to remove (the always-on receiver isn't installed)."
fi
osascript -e 'display notification "Always-on receiver removed" with title "Drawbridge"' >/dev/null 2>&1 || true

read -r -t 6 -p "(this window closes in 6s, or press Return) " _ || true
