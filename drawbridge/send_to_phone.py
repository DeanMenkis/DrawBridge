#!/usr/bin/env python3
"""Send one or more files from this Mac to the phone's LocalSend app.

Speaks LocalSend v2 over the local network. This is the "Mac -> phone"
half of Drawbridge.

Usage:
    python3 send_to_phone.py video1.mp4 [video2.mov ...]
    python3 send_to_phone.py --host 192.168.1.50 clip.mp4
    python3 send_to_phone.py --discover clip.mp4      # find the phone by itself

The phone must have the LocalSend app installed and open (or allowed to
receive in the background). Point --host at the phone's LAN IP, or set
phone.host in config.json, or use --discover to find it automatically.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import localsend

DEFAULT_CONFIG = str(Path(__file__).resolve().parent / "config.json")
LOG = logging.getLogger("drawbridge.send")


def load_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def discover_phone(mac_fingerprint: str, timeout: float = 4.0) -> str | None:
    """Listen briefly for a LocalSend multicast announce from a *mobile*
    device and return its IP, so you don't have to type the phone's address.
    Best-effort: returns None if nothing announces in time."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    try:
        sock.bind(("", localsend.MULTICAST_PORT))
        mreq = struct.pack(
            "4sl", socket.inet_aton(localsend.MULTICAST_GROUP), socket.INADDR_ANY
        )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError as exc:
        LOG.warning("discovery unavailable (%s); use --host instead", exc)
        sock.close()
        return None

    deadline = time.time() + timeout
    sock.settimeout(1.0)
    try:
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65536)
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                peer = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if peer.get("fingerprint") == mac_fingerprint:
                continue  # our own announce
            if peer.get("deviceType") in ("mobile", "web") or peer.get("alias"):
                LOG.info(
                    "discovered '%s' at %s", peer.get("alias", "?"), addr[0]
                )
                return addr[0]
    finally:
        sock.close()
    return None


def default_gateway() -> str | None:
    """Return this Mac's default-gateway IP. When the phone is acting as the
    Wi-Fi hotspot, the phone *is* the gateway, so this finds it with no config.
    (On a normal router this returns the router, which is why it's only used
    as a last resort, with a clear log line.)"""
    try:
        out = subprocess.run(
            ["route", "-n", "get", "default"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("gateway:"):
            gw = stripped.split(":", 1)[1].strip()
            return gw or None
    return None


def build_client(cfg: dict, host: str, protocol: str) -> localsend.LocalSendClient:
    mac = cfg.get("mac", {})
    tr = cfg.get("transfer", {})
    phone = cfg.get("phone", {})
    cert = key = None
    fingerprint = mac.get("fingerprint", "drawbridge-mac")
    if protocol == "https":
        # A peer with encryption ON requires a client certificate; its
        # sha256 is our identity in LocalSend's trust model.
        cert, key, fingerprint = localsend.ensure_client_certificate(
            Path(__file__).resolve().parent
        )
    info = localsend.make_info(
        mac.get("alias", "MacBook"),
        0,
        device_model="Mac",
        device_type="desktop",
        fingerprint=fingerprint,
        protocol=protocol,
    )
    return localsend.LocalSendClient(
        info,
        host,
        phone.get("port", 53317),
        protocol=protocol,
        chunk_size=tr.get("chunk_size_bytes", 1048576),
        timeout=tr.get("timeout_seconds", 600),
        retries=tr.get("retries", 3),
        backoff=tr.get("retry_backoff_seconds", 5),
        cert_file=cert,
        key_file=key,
        logger=LOG,
    )


def pick_client(cfg: dict, host: str) -> localsend.LocalSendClient:
    """Figure out how the phone wants to talk. 'auto' probes HTTPS (the
    LocalSend app's encryption-ON default) then plain HTTP; an explicit
    protocol in config is used as-is."""
    configured = cfg.get("phone", {}).get("protocol", "auto")
    candidates = ["https", "http"] if configured == "auto" else [configured]
    last = None
    for proto in candidates:
        client = build_client(cfg, host, proto)
        last = client
        peer = client.register(timeout=6)
        if peer is not None:
            LOG.info(
                "phone answered over %s: alias=%r", proto, peer.get("alias", "?")
            )
            return client
    LOG.warning(
        "phone did not answer register on %s -- attempting the transfer anyway "
        "via %s. Is LocalSend open on the phone?",
        "/".join(candidates),
        last.protocol,
    )
    return last


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Send files to the phone via LocalSend")
    parser.add_argument("files", nargs="+", help="file(s) to send")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--host", help="phone LAN IP (overrides config)")
    parser.add_argument(
        "--discover", action="store_true", help="auto-find the phone on the LAN"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    cfg = load_config(args.config)
    verify = cfg.get("transfer", {}).get("verify_sha256", True)
    mac_fp = cfg.get("mac", {}).get("fingerprint", "drawbridge-mac")

    host = args.host or cfg.get("phone", {}).get("host") or ""
    if args.discover or not host:
        found = discover_phone(mac_fp)
        if found:
            host = found
    if not host:
        gw = default_gateway()
        if gw:
            LOG.info(
                "no phone found by discovery; trying the gateway %s "
                "(this is your phone if it's the hotspot)",
                gw,
            )
            host = gw
    if not host:
        LOG.error(
            "No phone address. Open LocalSend on the phone, then either pass "
            "--host <phone-ip>, set phone.host in config.json, or use --discover."
        )
        return 2

    # Probe which protocol the phone speaks (encryption on = https) and
    # confirm it is reachable before streaming a large video.
    client = pick_client(cfg, host)

    failures = 0
    valid: list[Path] = []
    for f in args.files:
        path = Path(f).expanduser()
        if path.is_file():
            valid.append(path)
        else:
            LOG.error("not a file: %s", path)
            failures += 1

    if valid:
        total_mb = sum(p.stat().st_size for p in valid) / (1024 * 1024)
        LOG.info(
            "sending %d file(s), %.1f MB total -> %s (one batch, one prompt) ...",
            len(valid),
            total_mb,
            host,
        )
        try:
            # One LocalSend session for the whole selection: the phone sees a
            # single grouped transfer instead of N separate ones.
            results = client.send_files(valid, compute_sha256=verify)
        except Exception as exc:  # noqa: BLE001 - a batch-level failure (e.g.
            # prepare-upload transport error) must be reported, not raised at
            # the user from a right-click action.
            LOG.error("batch failed: %s", exc)
            failures += len(valid)
        else:
            for path, ok in results.items():
                if ok:
                    LOG.info("  delivered: %s", path.name)
                else:
                    LOG.error("  FAILED: %s", path.name)
                    failures += 1

    if failures:
        LOG.error("%d of %d file(s) failed.", failures, len(args.files))
        return 1
    LOG.info("all %d file(s) delivered.", len(args.files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
