#!/usr/bin/env python3
"""Receive files from the phone onto this Mac (the "phone -> Mac" half).

Runs a small LocalSend v2 server so the phone's LocalSend app sees this Mac
as a nearby device called "MacBook". Anything the phone sends lands in
save_dir (default ~/Drawbridge) and pops a macOS notification.

No Raspberry Pi, no radio. Just LocalSend over the home Wi-Fi/LAN.

Usage:
    python3 receive_on_mac.py                 # run in the foreground (Ctrl-C to stop)
    python3 receive_on_mac.py --dir ~/Movies  # override the save folder

To run it always-on in the background, install the LaunchAgent:
    see com.drawbridge.receiver.plist and the README.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import localsend

DEFAULT_CONFIG = str(Path(__file__).resolve().parent / "config.json")
LOG = logging.getLogger("drawbridge.receive")


def load_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def notify(title: str, message: str) -> None:
    """Best-effort macOS banner. Never let a notification failure crash the
    receiver (e.g. when run headless without a GUI session). Set
    DRAWBRIDGE_NO_NOTIFY=1 to suppress banners entirely."""
    import os

    if os.environ.get("DRAWBRIDGE_NO_NOTIFY"):
        return
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification {json.dumps(message)} with title {json.dumps(title)}',
            ],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Receive files from the phone via LocalSend")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--dir", help="save folder (overrides config save_dir)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
    )

    cfg = load_config(args.config)
    mac = cfg.get("mac", {})
    tr = cfg.get("transfer", {})

    save_dir = Path(args.dir or cfg.get("save_dir", "~/Drawbridge")).expanduser()
    save_dir.mkdir(parents=True, exist_ok=True)

    info = localsend.make_info(
        mac.get("alias", "MacBook"),
        mac.get("port", 53317),
        device_model="Mac",
        device_type="desktop",
        fingerprint=mac.get("fingerprint", "drawbridge-mac"),
        protocol=mac.get("protocol", "http"),
        download=True,
    )

    def on_received(path: Path) -> None:
        LOG.info("received %s", path)
        notify("Received from phone", path.name)

    server = localsend.LocalSendServer(
        info,
        save_dir,
        bind_host="0.0.0.0",
        port=mac.get("port", 53317),
        on_file_received=on_received,
        chunk_size=tr.get("chunk_size_bytes", 1048576),
        request_timeout=tr.get("timeout_seconds", 600),
        logger=LOG,
    )

    stop = threading.Event()

    def _handle(signum, _frame):
        LOG.info("stopping (signal %s)", signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    server.start()
    LOG.info(
        "Receiver ready as '%s' on port %s. Saving to %s. "
        "Open LocalSend on the phone and send to '%s'.",
        info["alias"],
        server.actual_port,
        save_dir,
        info["alias"],
    )
    try:
        while not stop.is_set():
            stop.wait(0.5)
    finally:
        server.stop()
        LOG.info("receiver stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
