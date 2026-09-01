<p align="center">
  <img src="assets/logo.svg" alt="Drawbridge" width="720">
</p>

# Drawbridge

AirDrop-style file sharing between a Mac and an Android phone. Right-click any file in Finder and send it. Files from the phone land in a folder on the Mac with a notification. No cloud, no cables, no app running on the Mac.

The phone runs the free [LocalSend](https://localsend.org) app. The Mac runs Drawbridge, a small pure-Python implementation of the LocalSend protocol wired into macOS: a Finder right-click menu for sending and a background service for receiving.

## Why not just run the LocalSend app on the Mac?

You can, and it is a good app. Drawbridge exists because the app still feels like an app, while AirDrop feels like part of the operating system. Drawbridge gives you the second thing:

1. **Right-click to send.** Select files in Finder, right-click, Send to Phone. No app to open, no window, no drag and drop. Multi-select is sent as one batch with a single prompt on the phone.
2. **Always-on receiving.** A login service receives files in the background forever, surviving reboots. There is no window to keep open and nothing to remember to launch. Files arrive in `~/Drawbridge` with a macOS notification.
3. **No runtime dependencies.** The whole thing is Python standard library, using the Python that ships with macOS. Nothing to install with brew or pip.
4. **Scriptable.** Sending is a plain command, so you can call it from scripts, Automator, or anything else.

It interoperates with the official app: encrypted (HTTPS with mutual TLS) or plain HTTP, chunked uploads, batched sessions, and discovery all match what the phone expects.

## Requirements

- macOS with its bundled Python 3 (tested on macOS 26)
- An Android phone with the LocalSend app (Play Store or F-Droid)
- Both devices on the same network. A phone hotspot works, including with no internet: Drawbridge finds the phone through the network gateway in that case

## Setup

Phone, one time:

1. Install LocalSend and open it.
2. In LocalSend settings, enable Quick Save if you want files from the Mac to be accepted automatically.
3. In Android settings, set LocalSend's battery usage to Unrestricted so Android does not kill it in the background.

Mac, one time:

1. Clone or download this repository.
2. Double-click `Install Right-Click Menu.command`. The first run of any `.command` file needs a right-click, Open, Open, because the scripts are unsigned.
3. Double-click `Install Always-On Receiver.command`.

Both installers copy the runtime to `~/Library/Application Support/drawbridge` and point macOS at it. This location matters: macOS privacy protection blocks background services from reading `~/Documents` and `~/Downloads`, which is also why received files go to `~/Drawbridge`.

## Use

Mac to phone: select any file or files in Finder, right-click, Quick Actions, Send to Phone. Have LocalSend open (or backgrounded, not swiped away) on the phone. macOS may ask once for permission to access the folder you selected from. Allow it.

Phone to Mac: in LocalSend, tap Send, pick files, choose the device named MacBook. Files arrive in `~/Drawbridge`.

Command line:

```bash
python3 drawbridge/send_to_phone.py movie.mp4 notes.pdf
python3 drawbridge/send_to_phone.py --host 192.168.1.50 big.zip
python3 drawbridge/receive_on_mac.py --dir ~/Movies
```

Configuration lives in `~/Library/Application Support/drawbridge/config.json` after install. The defaults work without editing. Fields you might touch:

| Field | Meaning |
|---|---|
| `phone.host` | The phone's IP. Leave empty to auto-discover, or pin it if discovery fails on your network |
| `phone.protocol` | `auto` probes HTTPS then HTTP to match the phone's encryption setting |
| `save_dir` | Where received files land. Default `~/Drawbridge` |
| `transfer.verify_sha256` | Hash outgoing files end to end. Off by default for interop with the official app |

## How it works

Drawbridge speaks LocalSend protocol v2: UDP multicast discovery on `224.0.0.167:53317`, then `register`, `prepare-upload`, and `upload` over HTTP(S) on port 53317. A multi-file selection is declared in one `prepare-upload` session so the receiver sees a single grouped transfer.

Details that took real debugging and are handled correctly here:

- The official app streams uploads with chunked transfer encoding and no Content-Length. The receiver decodes chunked bodies and verifies the byte count against the size declared in `prepare-upload`.
- With encryption on, the app's TLS stack (rustls) requires the client to present an X.509 **v3** certificate. The default `openssl req -x509` on macOS emits v1, which fails the handshake with a misleading `certificate unknown` alert. Drawbridge generates a v3 certificate and presents it, and its SHA-256 is the device fingerprint per the protocol.
- Every received file is streamed in bounded chunks to a `name.part` file that is atomically renamed only when complete, so an interrupted transfer never leaves a plausible-looking corrupt file. Same-name concurrent uploads reserve their destination with `O_CREAT | O_EXCL`, so they cannot interleave into one file.
- Failed sends retry with backoff, and one failed file in a batch does not abort the rest.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Right-click send does nothing | Read `/tmp/drawbridge-send.log`. Most often the phone's LocalSend is not open, or the devices are on different networks |
| Nothing arrives on the Mac | `launchctl list \| grep drawbridge` should show a `0`. Log is at `/tmp/drawbridge-receiver.log` |
| Send fails with encryption on | Update to the current Drawbridge (v3 certificate), or turn encryption off in LocalSend settings |
| Phone not found automatically | Set `phone.host` in the config to the IP shown on LocalSend's settings screen |

## Security notes

- The TLS key pair is generated locally on first use and stays in `~/Library/Application Support/drawbridge`. It is never committed; `.gitignore` covers it.
- In HTTP mode transfers are cleartext on your local network. Use encryption on both ends, or a hotspot containing only your own devices, if that matters for your files.
- The receiver accepts files from any LocalSend sender on your network. Do not run it on networks you do not trust.

## License

MIT. See [LICENSE](LICENSE).

Drawbridge is an independent project. It is not affiliated with LocalSend or Apple. AirDrop is a trademark of Apple Inc., used here only to describe the experience.
