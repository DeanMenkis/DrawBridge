#!/bin/bash
# Double-click once to add a "Send to Phone" item to Finder's right-click menu.
# After this: select any file(s) in Finder, right-click -> Quick Actions
# (or Services) -> Send to Phone. No folder to open, no .command to click.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICES="$HOME/Library/Services"
WF="$SERVICES/Send to Phone.workflow"
CONTENTS="$WF/Contents"

# macOS blocks Finder's action-runner from reading ~/Documents (privacy), so
# the runtime must live in a non-gated location. Deploy it to App Support and
# point the menu item there. config.json is only copied if absent, so edits
# made in the deployed copy survive re-installs.
DEPLOY="$HOME/Library/Application Support/drawbridge"
mkdir -p "$DEPLOY"
cp "$DIR/drawbridge/send_to_phone.py" "$DIR/drawbridge/receive_on_mac.py" "$DIR/drawbridge/localsend.py" "$DEPLOY/"
[ -f "$DEPLOY/config.json" ] || cp "$DIR/drawbridge/config.example.json" "$DEPLOY/config.json"

mkdir -p "$CONTENTS"

# --- Info.plist: declares the Finder service, accepts any file ---
cat > "$CONTENTS/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>NSServices</key>
    <array>
        <dict>
            <key>NSMenuItem</key>
            <dict>
                <key>default</key>
                <string>Send to Phone</string>
            </dict>
            <key>NSMessage</key>
            <string>runWorkflowAsService</string>
            <key>NSRequiredContext</key>
            <dict>
                <key>NSApplicationIdentifier</key>
                <string>com.apple.finder</string>
            </dict>
            <key>NSSendFileTypes</key>
            <array>
                <string>public.item</string>
            </array>
        </dict>
    </array>
</dict>
</plist>
PLIST

# --- document.wflow: a "Run Shell Script" action, input passed as arguments ---
# The shell the Quick Action runs: hand the selected files to the tested sender.
# Built with Python's plistlib so all XML escaping (e.g. the & in 2>&1) is correct.
SEND_SCRIPT="$DEPLOY/send_to_phone.py" python3 - "$CONTENTS/document.wflow" <<'PY'
import os, plistlib, sys, uuid

send = os.environ["SEND_SCRIPT"]
command = (
    '/usr/bin/python3 "%s" --discover "$@" > /tmp/drawbridge-send.log 2>&1\n'
    'if [ $? -eq 0 ]; then\n'
    "  osascript -e 'display notification \"Sent to your phone\" with title \"Drawbridge\"'\n"
    'else\n'
    "  osascript -e 'display notification \"Send failed - is LocalSend open on the phone and on the same Wi-Fi?\" with title \"Drawbridge\"'\n"
    'fi\n'
) % send

action = {
    "AMAccepts": {"Container": "List", "Optional": True, "Types": ["com.apple.cocoa.string"]},
    "AMActionVersion": "2.0.3",
    "AMApplication": ["Automator"],
    "AMParameterProperties": {
        "COMMAND_STRING": {}, "CheckedForUserDefaultShell": {},
        "inputMethod": {}, "shell": {}, "source": {},
    },
    "AMProvides": {"Container": "List", "Types": ["com.apple.cocoa.string"]},
    "ActionBundlePath": "/System/Library/Automator/Run Shell Script.action",
    "ActionName": "Run Shell Script",
    "ActionParameters": {
        "COMMAND_STRING": command,
        "CheckedForUserDefaultShell": True,
        "inputMethod": 1,  # 1 = pass the selected files as arguments ($@)
        "shell": "/bin/bash",
        "source": "",
    },
    "BundleIdentifier": "com.apple.RunShellScript",
    "CFBundleVersion": "2.0.3",
    "CanShowSelectedItemsWhenRun": False,
    "CanShowWhenRun": True,
    "Category": ["AMCategoryUtilities"],
    "Class Name": "RunShellScriptAction",
    "InputUUID": str(uuid.uuid4()).upper(),
    "Keywords": ["Shell", "Script", "Command", "Run", "Unix"],
    "OutputUUID": str(uuid.uuid4()).upper(),
    "UUID": str(uuid.uuid4()).upper(),
    "UnlocalizedApplications": ["Automator"],
    "location": "309.000000:253.000000",
    "nibPath": "/System/Library/Automator/Run Shell Script.action/Contents/Resources/main.nib",
    "isViewVisible": 1,
}

doc = {
    "AMApplicationBuild": "523",
    "AMApplicationVersion": "2.10",
    "AMDocumentVersion": "2",
    "actions": [{"action": action, "isViewVisible": 1}],
    "connectors": {},
    "workflowMetaData": {
        "applicationBundleIDsByProvider": {},
        "applicationPaths": [],
        "inputTypeIdentifier": "com.apple.Automator.fileSystemObject",
        "outputTypeIdentifier": "com.apple.Automator.nothing",
        "presentationMode": 15,
        "processesInput": 0,
        "serviceApplicationBundleID": "com.apple.finder",
        "serviceApplicationPath": "/System/Library/CoreServices/Finder.app",
        "serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject",
        "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
        "systemImageName": "NSActionTemplate",
        "useAutomaticInputType": 0,
        "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
    },
}

with open(sys.argv[1], "wb") as fh:
    plistlib.dump(doc, fh)
PY

# Validate what we generated.
plutil -lint "$CONTENTS/Info.plist" >/dev/null
plutil -lint "$CONTENTS/document.wflow" >/dev/null

# Refresh the macOS Services database so the item shows up.
/System/Library/CoreServices/pbs -update 2>/dev/null || true
/System/Library/CoreServices/pbs -flush 2>/dev/null || true

echo "Installed 'Send to Phone' into your right-click menu."
echo
echo "Use it: select file(s) in Finder -> right-click -> Quick Actions"
echo "(or the Services submenu) -> Send to Phone."
echo
echo "If it doesn't appear right away: open a NEW Finder window, or log out and"
echo "back in once. You can also enable it in System Settings -> Keyboard ->"
echo "Keyboard Shortcuts -> Services -> Files and Folders -> Send to Phone."
echo
echo "To remove it later, delete: ~/Library/Services/Send to Phone.workflow"
osascript -e 'display notification "Right-click menu installed" with title "Drawbridge"' >/dev/null 2>&1 || true

read -r -t 10 -p "(this window closes in 10s, or press Return) " _ || true
