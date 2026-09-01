#!/bin/bash
# Double-click to make this Mac ALWAYS ready to receive from the phone -- no
# window to keep open, and it comes back after a reboot. It runs quietly in the
# background and shows a notification whenever a file arrives.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/com.drawbridge.receiver.plist"

# macOS blocks background services from reading ~/Documents (privacy), so the
# runtime must live in a non-gated location. Deploy to App Support and run from
# there. config.json is only copied if absent, so your edits survive.
DEPLOY="$HOME/Library/Application Support/drawbridge"
mkdir -p "$DEPLOY"
cp "$DIR/drawbridge/send_to_phone.py" "$DIR/drawbridge/receive_on_mac.py" "$DIR/drawbridge/localsend.py" "$DEPLOY/"
[ -f "$DEPLOY/config.json" ] || cp "$DIR/drawbridge/config.example.json" "$DEPLOY/config.json"

mkdir -p "$AGENTS"

# Write the LaunchAgent with the real absolute paths to THIS folder.
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.drawbridge.receiver</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>${DEPLOY}/receive_on_mac.py</string>
        <string>--config</string>
        <string>${DEPLOY}/config.json</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/drawbridge-receiver.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/drawbridge-receiver.log</string>
</dict>
</plist>
PLISTEOF

# Sanity-check the file we just wrote, then (re)load it.
plutil -lint "$PLIST" >/dev/null
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "Installed. This Mac now receives from the phone in the background,"
echo "even after a reboot. Files save to ~/Drawbridge."
echo "It appears to the phone as 'MacBook' in LocalSend."
echo "Log file: /tmp/drawbridge-receiver.log"
echo "To turn it off, double-click 'Uninstall Always-On Receiver.command'."
osascript -e 'display notification "Always-on receiver is running" with title "Drawbridge"' >/dev/null 2>&1 || true

read -r -t 8 -p "(this window closes in 8s, or press Return) " _ || true
