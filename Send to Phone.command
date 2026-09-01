#!/bin/bash
# Double-click this to pick ANY file(s) with the built-in macOS Finder chooser
# and send them to your phone (which is running the LocalSend app).
# No Raspberry Pi, no radio -- just your home Wi-Fi.
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

# 1) Open the native macOS file-selection panel (the built-in Finder browser).
#    Any file type; multiple selection allowed. Returns one POSIX path per line,
#    or empty if you cancel.
PATHS="$(osascript <<'APPLESCRIPT'
try
    set theFiles to choose file with prompt "Pick file(s) to send to your phone:" with multiple selections allowed
on error number -128
    return ""
end try
set out to ""
repeat with f in theFiles
    set out to out & POSIX path of f & linefeed
end repeat
return out
APPLESCRIPT
)"

# 2) Bail out quietly if nothing was chosen.
if [ -z "${PATHS//[$'\n']/}" ]; then
    echo "Cancelled - nothing sent."
    exit 0
fi

# 3) Read the chosen paths into an array (one per line; handles spaces).
files=()
while IFS= read -r line; do
    [ -n "$line" ] && files+=("$line")
done <<< "$PATHS"

echo "Sending ${#files[@]} file(s) to your phone..."

# 4) Hand them to the tested sender. --discover auto-finds the phone on the LAN;
#    if that fails it falls back to phone.host in config.json.
if python3 "$DIR/drawbridge/send_to_phone.py" --discover "${files[@]}"; then
    osascript -e 'display notification "Sent to your phone" with title "Drawbridge"' >/dev/null 2>&1
    echo "Done."
else
    osascript -e 'display dialog "Some files did not send.\n\nCheck that LocalSend is open on your phone and both are on the same Wi-Fi." buttons {"OK"} default button "OK" with icon caution' >/dev/null 2>&1
    echo "Some files failed - see messages above."
fi

# Keep the Terminal window readable for a moment when double-clicked.
echo
read -r -t 8 -p "(this window closes in 8s, or press Return) " _ || true
