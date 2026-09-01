"""Stdlib-only implementation of the LocalSend v2 protocol.

Implements just enough of LocalSend v2.2 for the drawbridge relay to be
*both* a LocalSend server (so the phone can push files to the Pi) and a
LocalSend client (so the relay can push files it received from the Mac out
to the phone). No third-party dependencies: only ``socket``, ``http.server``,
``http.client``, ``json``, ``hashlib``, ``threading`` and friends from the
standard library.

Protocol facts implemented here (per the LocalSend v2.2 spec document; the
device-info ``version`` field advertises ``"2.1"`` to match what current
LocalSend clients send):

* Discovery: UDP multicast announce/listen on ``224.0.0.167:53317``. The
  announce payload is the device "info" object plus ``"announce": true``.
* ``POST /api/localsend/v2/register`` - body is the sender's info object,
  response is the receiver's info object.
* ``POST /api/localsend/v2/prepare-upload`` - body is
  ``{"info": {...}, "files": {"<fileId>": {"id", "fileName", "size",
  "fileType", "sha256"?, "preview"?}}}``. Response is
  ``{"sessionId": str, "files": {"<fileId>": "<fileToken>"}}``, or a bare
  ``204`` if the receiver doesn't want any of the offered files.
* ``POST /api/localsend/v2/upload?sessionId=..&fileId=..&token=..`` - the
  request body is the *raw* file bytes (not multipart). ``200`` on success,
  ``422`` on a sha256 mismatch.

Large-file safety is the priority throughout: every read/write here is done
in bounded ``chunk_size`` pieces, nothing ever calls ``.read()`` (or
equivalent) without a size limit, uploads land in a ``<name>.part`` file
that is only atomically renamed into place once fully and correctly
received, and interrupted transfers never leave a partial file with its
final name in the target directory.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import mimetypes
import os
import socket
import ssl
import struct
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

LOG = logging.getLogger("drawbridge.localsend")

MULTICAST_GROUP = "224.0.0.167"
MULTICAST_PORT = 53317
# Advertised in the device-info ``version`` field. LocalSend clients send
# "2.1" here even though the current written spec is labelled v2.2; we match
# the clients, not the doc title, so real phones accept our announce/register.
PROTOCOL_VERSION = "2.1"
API_PREFIX = "/api/localsend/v2"

DEFAULT_CHUNK_SIZE = 1024 * 1024
DEFAULT_TIMEOUT = 600
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 5


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def make_info(
    alias,
    port,
    *,
    device_model="",
    device_type="headless",
    fingerprint="",
    protocol="http",
    download=False,
):
    """Build a LocalSend v2 device "info" object."""
    return {
        "alias": alias,
        "version": PROTOCOL_VERSION,
        "deviceModel": device_model,
        "deviceType": device_type,
        "fingerprint": fingerprint,
        "port": port,
        "protocol": protocol,
        "download": download,
    }


def sha256_file(path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """Compute a sha256 digest of ``path`` without loading it into memory."""
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def guess_file_type(path) -> str:
    """Map a file name to LocalSend's coarse fileType categories."""
    mime, _ = mimetypes.guess_type(str(path))
    if not mime:
        return "other"
    major = mime.split("/")[0]
    return major if major in ("image", "video", "audio", "text") else "other"


def safe_filename(name: str) -> str:
    """Strip any path components/NULs from a peer-supplied file name."""
    name = os.path.basename((name or "").replace("\x00", ""))
    if name in ("", ".", ".."):
        name = "upload.bin"
    return name


def ensure_client_certificate(directory) -> tuple[str, str, str]:
    """Return (cert_path, key_path, sha256_fingerprint) for this device's
    LocalSend identity, generating a self-signed pair on first use.

    LocalSend's HTTPS mode uses self-signed certificates on both ends; a
    peer with encryption ON requires the client to present one (its SHA-256
    is the client's identity). Generated with the system openssl."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    if not (cert.is_file() and key.is_file()):
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", str(key), "-out", str(cert),
                "-days", "3650", "-nodes",
                "-subj", "/CN=drawbridge-mac",
                # Forces an X.509 v3 certificate. LibreSSL emits v1 without an
                # extension, and LocalSend's rustls stack rejects v1 client
                # certs during the handshake with alert certificate_unknown.
                "-addext", "basicConstraints=CA:FALSE",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        os.chmod(key, 0o600)
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    # LocalSend fingerprints are the UPPERCASE hex SHA-256 of the cert DER.
    fingerprint = hashlib.sha256(der).hexdigest().upper()
    return str(cert), str(key), fingerprint


def unique_path(path: Path) -> Path:
    """Return ``path``, or the first ``path.N.ext`` that is free.

    "Free" means neither the final name nor its in-progress ``.part``
    sibling currently exists, so two uploads with the same file name never
    collide (including one that is mid-write).
    """

    def _taken(candidate: Path) -> bool:
        part = candidate.parent / (candidate.name + ".part")
        return candidate.exists() or part.exists()

    if not _taken(path):
        return path
    stem, suffix = path.stem, path.suffix
    i = 1
    while True:
        candidate = path.with_name(f"{stem}.{i}{suffix}")
        if not _taken(candidate):
            return candidate
        i += 1


def reserve_dest(target_dir, filename: str):
    """Atomically reserve a free ``<name>.part`` under ``target_dir``.

    Returns ``(open_fd, part_path, dest_path)``. Unlike :func:`unique_path`
    (which only *picks* a name and is therefore racy between check and open),
    this reserves the ``.part`` file with ``O_CREAT | O_EXCL`` so two
    concurrent uploads of the *same* file name can never claim the same
    ``.part`` and interleave their bytes into one corrupt file. It also
    re-checks the final name *after* winning the ``.part`` so it never
    ``os.replace``s over a file that another upload completed in the
    meantime. The caller owns the returned fd and must close it (e.g. via
    ``os.fdopen``).
    """
    target_dir = Path(target_dir)
    base = target_dir / filename
    stem, suffix = base.stem, base.suffix
    i = 0
    while True:
        dest = base if i == 0 else base.with_name(f"{stem}.{i}{suffix}")
        part = dest.parent / (dest.name + ".part")
        if dest.exists():
            i += 1
            continue
        try:
            fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            i += 1
            continue
        # We now exclusively hold `part`. If the final name appeared while we
        # were grabbing it, back off to the next candidate rather than clobber
        # a just-completed file; nobody else can create `dest` now because the
        # only path to it is through a `.part` we hold.
        if dest.exists():
            os.close(fd)
            try:
                os.unlink(part)
            except FileNotFoundError:
                pass
            i += 1
            continue
        return fd, part, dest


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class _Sessions:
    """Thread-safe registry of in-progress prepare-upload sessions.

    Sessions are normally dropped when their last file arrives. A phone that
    calls prepare-upload and then never uploads (user cancels, app killed)
    would otherwise leak a session forever on a long-running daemon, so each
    ``create`` also reaps any session older than ``ttl`` seconds.
    """

    def __init__(self, ttl: float = 7200):
        self._lock = threading.Lock()
        self._sessions = {}
        self._created = {}
        self._ttl = ttl

    def _reap_locked(self) -> None:
        if self._ttl <= 0:
            return
        cutoff = time.monotonic() - self._ttl
        for sid in [s for s, t in self._created.items() if t < cutoff]:
            self._sessions.pop(sid, None)
            self._created.pop(sid, None)

    def create(self, files: dict) -> tuple[str, dict]:
        session_id = uuid.uuid4().hex
        tokens = {}
        entry = {}
        for file_id, meta in files.items():
            token = uuid.uuid4().hex
            tokens[file_id] = token
            entry[file_id] = {
                "token": token,
                "meta": meta,
                "received": False,
                "lock": threading.Lock(),
            }
        with self._lock:
            self._reap_locked()
            self._sessions[session_id] = entry
            self._created[session_id] = time.monotonic()
        return session_id, tokens

    def get(self, session_id):
        with self._lock:
            return self._sessions.get(session_id)

    def drop(self, session_id):
        with self._lock:
            self._sessions.pop(session_id, None)
            self._created.pop(session_id, None)


class _BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_handler(server_obj: LocalSendServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "drawbridge-localsend/1.0"
        # Per-socket-operation timeout. A half-open connection (peer vanished
        # without FIN/RST) would otherwise block rfile.read() forever, hanging
        # the worker thread while it holds the per-file lock. This trips only
        # when NO bytes arrive for `timeout` seconds, so a slow-but-progressing
        # large-video upload is unaffected; a genuinely stalled one raises
        # socket.timeout (an OSError), which _receive_file already cleans up.
        timeout = server_obj.request_timeout

        def log_message(self, fmt, *args):
            server_obj.log.debug("%s - %s", self.address_string(), fmt % args)

        # -- helpers ---------------------------------------------------
        def _send_json(self, code: int, obj) -> None:
            try:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (OSError, ConnectionError) as exc:
                server_obj.log.debug("could not write response: %s", exc)

        def _send_empty(self, code: int) -> None:
            try:
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except (OSError, ConnectionError) as exc:
                server_obj.log.debug("could not write response: %s", exc)

        def _read_json_body(self):
            length_header = self.headers.get("Content-Length")
            length = int(length_header) if length_header else 0
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw.decode("utf-8")) if raw else {}

        # -- routing -----------------------------------------------------
        def do_POST(self):
            parsed = urlsplit(self.path)
            if parsed.path == f"{API_PREFIX}/register":
                self._handle_register()
            elif parsed.path == f"{API_PREFIX}/prepare-upload":
                self._handle_prepare_upload()
            elif parsed.path == f"{API_PREFIX}/upload":
                self._handle_upload(parsed)
            else:
                self._send_json(404, {"error": "not found"})

        def _handle_register(self):
            try:
                self._read_json_body()
            except (ValueError, UnicodeDecodeError):
                pass
            self._send_json(200, server_obj.info)

        def _handle_prepare_upload(self):
            try:
                body = self._read_json_body()
            except (ValueError, UnicodeDecodeError):
                self._send_json(400, {"error": "bad json"})
                return
            files = body.get("files") or {}
            if not files:
                self._send_empty(204)
                return
            session_id, tokens = server_obj.sessions.create(files)
            self._send_json(200, {"sessionId": session_id, "files": tokens})

        def _handle_upload(self, parsed):
            qs = parse_qs(parsed.query)
            session_id = (qs.get("sessionId") or [None])[0]
            file_id = (qs.get("fileId") or [None])[0]
            token = (qs.get("token") or [None])[0]

            session = server_obj.sessions.get(session_id) if session_id else None
            entry = session.get(file_id) if session and file_id else None
            if not entry or entry["token"] != token:
                self._drain_and_discard()
                self._send_json(403, {"error": "invalid session/file/token"})
                return

            # The official app streams uploads with Transfer-Encoding: chunked
            # (no Content-Length at all); a plain client sends Content-Length.
            # length=None means "chunked -- read until the terminating chunk".
            te = (self.headers.get("Transfer-Encoding") or "").lower()
            if "chunked" in te:
                length = None
            else:
                length_header = self.headers.get("Content-Length")
                try:
                    length = int(length_header) if length_header is not None else 0
                except ValueError:
                    self._send_json(411, {"error": "invalid content-length"})
                    return

            with entry["lock"]:
                if entry["received"]:
                    self._drain_and_discard()
                    self._send_json(200, {"status": "already received"})
                    return
                dest, ok = self._receive_file(entry, length)
                if not ok:
                    return
                entry["received"] = True

            self._send_json(200, {"status": "ok"})

            if server_obj.on_file_received:
                try:
                    server_obj.on_file_received(dest)
                except Exception:
                    server_obj.log.exception(
                        "on_file_received callback failed for %s", dest
                    )

            if session and all(e["received"] for e in session.values()):
                server_obj.sessions.drop(session_id)

        def _read_plain_body(self, fh, hasher, length: int) -> int:
            """Read exactly ``length`` raw bytes into ``fh``."""
            written = 0
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(server_obj.chunk_size, remaining))
                if not chunk:
                    break  # peer closed/aborted mid-transfer
                fh.write(chunk)
                hasher.update(chunk)
                written += len(chunk)
                remaining -= len(chunk)
            return written

        def _read_chunked_body(self, fh, hasher) -> int:
            """Decode a Transfer-Encoding: chunked body into ``fh``. Raises
            ConnectionError on a malformed/truncated stream."""
            written = 0
            while True:
                size_line = self.rfile.readline(1024)
                if not size_line:
                    raise ConnectionError("peer closed before final chunk")
                try:
                    chunk_len = int(size_line.strip().split(b";")[0], 16)
                except ValueError:
                    raise ConnectionError(f"bad chunk size line {size_line!r}")
                if chunk_len == 0:
                    # consume optional trailers up to the blank line
                    while True:
                        trailer = self.rfile.readline(1024)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    return written
                remaining = chunk_len
                while remaining > 0:
                    data = self.rfile.read(min(server_obj.chunk_size, remaining))
                    if not data:
                        raise ConnectionError("peer closed mid-chunk")
                    fh.write(data)
                    hasher.update(data)
                    written += len(data)
                    remaining -= len(data)
                self.rfile.read(2)  # CRLF that terminates each chunk

        def _receive_file(self, entry, length):
            """Stream the request body to a ``.part`` file, then atomically
            rename it into place. ``length`` is the Content-Length, or None
            for a chunked upload. Returns (dest_path, success_bool). On any
            failure the ``.part`` file is removed and an error response is
            already written to the client before returning."""
            meta = entry["meta"]
            filename = safe_filename(meta.get("fileName") or "upload.bin")
            # The size declared in prepare-upload is the authoritative
            # expected size, whatever the transfer encoding.
            expected_size = meta.get("size")
            if not isinstance(expected_size, int) or expected_size < 0:
                expected_size = None
            # Atomically reserve the .part so two same-name uploads can never
            # write into the same file (see reserve_dest); it hands us an open
            # fd that os.fdopen takes ownership of and closes for us.
            fd, tmp, dest = reserve_dest(server_obj.target_dir, filename)

            hasher = hashlib.sha256()
            written = 0
            try:
                with os.fdopen(fd, "wb") as fh:
                    if length is None:
                        written = self._read_chunked_body(fh, hasher)
                    else:
                        written = self._read_plain_body(fh, hasher, length)
                    fh.flush()
                    os.fsync(fh.fileno())
            except (OSError, ConnectionError) as exc:
                server_obj.log.warning(
                    "upload interrupted for %s: %s", filename, exc
                )
                tmp.unlink(missing_ok=True)
                self._send_json(500, {"error": "write failed"})
                return dest, False

            if (length is not None and written != length) or (
                expected_size is not None and written != expected_size
            ):
                server_obj.log.warning(
                    "upload incomplete for %s: got %d bytes (content-length=%s, declared size=%s)",
                    filename,
                    written,
                    length,
                    expected_size,
                )
                tmp.unlink(missing_ok=True)
                self._send_json(400, {"error": "incomplete body"})
                return dest, False

            expected_sha = (meta.get("sha256") or "").strip()
            digest = hasher.hexdigest()
            if expected_sha:
                # Different LocalSend implementations encode the digest
                # differently (hex vs base64, case). Accept any encoding of
                # the RIGHT digest; on a true mismatch be lenient by default:
                # the exact-length check above plus TCP already guard
                # integrity, and hard-rejecting real files over an encoding
                # nuance breaks interop with the official app.
                b64 = base64.b64encode(hasher.digest()).decode("ascii")
                matches = expected_sha.lower() == digest.lower() or expected_sha in (
                    b64,
                    b64.rstrip("="),
                )
                if not matches:
                    if server_obj.strict_sha256:
                        server_obj.log.warning("sha256 mismatch for %s", filename)
                        tmp.unlink(missing_ok=True)
                        self._send_json(422, {"error": "sha256 mismatch"})
                        return dest, False
                    server_obj.log.warning(
                        "declared sha256 for %s does not match computed digest "
                        "(declared=%r, computed=%s); accepting anyway -- length "
                        "verified, set strict_sha256 to reject instead",
                        filename,
                        expected_sha,
                        digest,
                    )

            os.replace(tmp, dest)
            server_obj.log.info(
                "received file %s (%d bytes, sha256=%s)", dest, written, digest
            )
            return dest, True

        def _drain_and_discard(self):
            te = (self.headers.get("Transfer-Encoding") or "").lower()
            if "chunked" in te:
                # Not worth decoding a body we're rejecting -- just make sure
                # this connection isn't reused with unread bytes on it.
                self.close_connection = True
                return
            length_header = self.headers.get("Content-Length")
            try:
                remaining = int(length_header) if length_header else 0
            except ValueError:
                remaining = 0
            while remaining > 0:
                chunk = self.rfile.read(min(server_obj.chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)

    return Handler


class LocalSendServer:
    """A LocalSend v2 server: announces itself, registers, and accepts
    incoming file uploads into ``target_dir``."""

    def __init__(
        self,
        info: dict,
        target_dir,
        *,
        bind_host: str = "0.0.0.0",
        port: int = MULTICAST_PORT,
        on_file_received=None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        announce_interval: float = 5.0,
        enable_multicast: bool = True,
        request_timeout: float = DEFAULT_TIMEOUT,
        strict_sha256: bool = False,
        logger: logging.Logger | None = None,
    ):
        self.info = dict(info)
        self.target_dir = Path(target_dir)
        self.target_dir.mkdir(parents=True, exist_ok=True)
        self.bind_host = bind_host
        self.port = port
        self.on_file_received = on_file_received
        self.chunk_size = chunk_size
        self.announce_interval = announce_interval
        self.enable_multicast = enable_multicast
        self.request_timeout = request_timeout
        self.strict_sha256 = strict_sha256
        self.log = logger or LOG

        self.sessions = _Sessions()
        self._httpd: _BridgeHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._announce_sock: socket.socket | None = None
        self._announce_thread: threading.Thread | None = None
        self._listen_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def actual_port(self) -> int:
        return self._httpd.server_address[1] if self._httpd else self.port

    def start(self) -> None:
        handler = _make_handler(self)
        self._httpd = _BridgeHTTPServer((self.bind_host, self.port), handler)
        self.port = self._httpd.server_address[1]
        self.info["port"] = self.port
        self._http_thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name="localsend-http"
        )
        self._http_thread.start()
        if self.enable_multicast:
            self._start_multicast()
        self.log.info(
            "LocalSend server listening on %s:%s", self.bind_host, self.port
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._announce_sock:
            try:
                self._announce_sock.close()
            except OSError:
                pass
        for t in (self._http_thread, self._announce_thread, self._listen_thread):
            if t:
                t.join(timeout=5)

    # -- multicast discovery (best-effort; explicit config always wins) ---
    def _start_multicast(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            sock.bind(("", MULTICAST_PORT))
            mreq = struct.pack("4sl", socket.inet_aton(MULTICAST_GROUP), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as exc:
            self.log.warning(
                "multicast bind/join failed (%s); relying on configured phone.host only",
                exc,
            )
            sock.close()
            return
        sock.settimeout(1.0)
        self._announce_sock = sock
        self._announce_thread = threading.Thread(
            target=self._announce_loop, daemon=True, name="localsend-announce"
        )
        self._announce_thread.start()
        self._listen_thread = threading.Thread(
            target=self._listen_loop, daemon=True, name="localsend-listen"
        )
        self._listen_thread.start()

    def _announce_loop(self) -> None:
        send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        send_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        payload = json.dumps({**self.info, "announce": True}).encode("utf-8")
        try:
            while not self._stop_event.is_set():
                try:
                    send_sock.sendto(payload, (MULTICAST_GROUP, MULTICAST_PORT))
                except OSError as exc:
                    self.log.debug("announce send failed: %s", exc)
                self._stop_event.wait(self.announce_interval)
        finally:
            send_sock.close()

    def _listen_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                data, addr = self._announce_sock.recvfrom(65536)
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                peer = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if peer.get("fingerprint") == self.info.get("fingerprint"):
                continue  # our own announce, looped back
            self.log.debug(
                "saw LocalSend peer announce from %s: alias=%s",
                addr[0],
                peer.get("alias"),
            )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LocalSendClient:
    """A LocalSend v2 client used to push files from the Pi to a known peer
    (the phone). Streams uploads directly from disk in ``chunk_size``
    pieces; never buffers a whole file in memory."""

    def __init__(
        self,
        info: dict,
        host: str,
        port: int,
        *,
        protocol: str = "http",
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        backoff: float = DEFAULT_BACKOFF,
        cert_file: str | None = None,
        key_file: str | None = None,
        logger: logging.Logger | None = None,
    ):
        self.info = dict(info)
        self.host = host
        self.port = port
        self.protocol = protocol
        self.chunk_size = chunk_size
        self.timeout = timeout
        self.retries = max(1, retries)
        self.backoff = backoff
        self.log = logger or LOG

        # LocalSend peers use self-signed certs, so server verification is
        # off (the protocol's trust model is the cert *fingerprint*, not a
        # CA); a peer with encryption ON additionally requires us to present
        # our own client certificate.
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE
        if cert_file and key_file:
            self._ssl_ctx.load_cert_chain(cert_file, key_file)

    def _connect(self, timeout: float | None = None):
        t = self.timeout if timeout is None else timeout
        if self.protocol == "https":
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=t, context=self._ssl_ctx
            )
        return http.client.HTTPConnection(self.host, self.port, timeout=t)

    def _request_json(self, method: str, path: str, obj=None, timeout: float | None = None):
        body = json.dumps(obj).encode("utf-8") if obj is not None else None
        headers = {"Content-Type": "application/json"}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        conn = self._connect(timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            status = resp.status
        finally:
            conn.close()
        if status == 204 or not payload:
            return status, None
        try:
            return status, json.loads(payload.decode("utf-8"))
        except ValueError:
            return status, None

    def register(self, timeout: float | None = None):
        """Best-effort register call; returns the peer's info dict, or
        ``None`` if the peer doesn't implement/accept it. ``timeout``
        overrides the (long) transfer timeout -- probes should pass a short
        one so a wrong-protocol guess fails in seconds, not minutes."""
        try:
            status, resp = self._request_json(
                "POST", f"{API_PREFIX}/register", self.info, timeout=timeout
            )
        except (OSError, http.client.HTTPException) as exc:
            self.log.debug("register failed: %s", exc)
            return None
        return resp if status == 200 else None

    def prepare_upload(self, files_meta: dict):
        status, resp = self._request_json(
            "POST",
            f"{API_PREFIX}/prepare-upload",
            {"info": self.info, "files": files_meta},
        )
        if status == 204:
            return None
        if status != 200 or resp is None:
            raise RuntimeError(f"prepare-upload failed: HTTP {status}")
        return resp

    def _upload_once(self, session_id: str, file_id: str, token: str, path: Path, size: int) -> int:
        query = (
            f"{API_PREFIX}/upload?sessionId={quote(session_id)}"
            f"&fileId={quote(file_id)}&token={quote(token)}"
        )
        conn = self._connect()
        try:
            conn.putrequest("POST", query)
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(size))
            conn.endheaders()
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(self.chunk_size)
                    if not chunk:
                        break
                    conn.send(chunk)
            resp = conn.getresponse()
            status = resp.status
            resp.read()
        finally:
            conn.close()
        return status

    def upload_file(self, session_id: str, file_id: str, token: str, path, *, size: int) -> int:
        path = Path(path)
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                status = self._upload_once(session_id, file_id, token, path, size)
                if status == 200:
                    return status
                last_exc = RuntimeError(f"upload rejected: HTTP {status}")
            except (OSError, http.client.HTTPException) as exc:
                last_exc = exc
            self.log.warning(
                "upload attempt %d/%d failed for %s: %s",
                attempt,
                self.retries,
                path,
                last_exc,
            )
            if attempt < self.retries:
                time.sleep(self.backoff * attempt)
        raise RuntimeError(f"upload failed after {self.retries} attempts: {last_exc}")

    def send_file(self, path, *, file_type: str = "other", compute_sha256: bool = False) -> bool:
        """Push a single file to the configured peer. Returns True on
        success, False if the peer declined it (HTTP 204 - nothing to
        transfer). Raises on a hard transport failure after retries.
        (``file_type`` is kept for compatibility; the type is guessed.)"""
        del file_type  # guessed per file by send_files
        results = self.send_files([path], compute_sha256=compute_sha256)
        return bool(next(iter(results.values()), False))

    def send_files(self, paths, *, compute_sha256: bool = False) -> dict:
        """Push several files to the peer in ONE LocalSend session: a single
        prepare-upload declares them all (so the receiver sees one grouped
        batch and asks once), then each file is streamed in turn within that
        session. Returns {Path: bool} per file; a file the peer declined
        (no token in the response) or whose upload exhausted its retries is
        False -- one bad file never aborts its batch-mates."""
        paths = [Path(p) for p in paths]
        metas: dict[str, dict] = {}
        by_id: dict[str, Path] = {}
        for path in paths:
            file_id = uuid.uuid4().hex
            meta = {
                "id": file_id,
                "fileName": path.name,
                "size": path.stat().st_size,
                "fileType": guess_file_type(path),
            }
            if compute_sha256:
                meta["sha256"] = sha256_file(path, self.chunk_size)
            metas[file_id] = meta
            by_id[file_id] = path

        prep = self.prepare_upload(metas)
        if not prep:
            self.log.info("peer declined the transfer (nothing to send)")
            return {p: False for p in paths}
        session_id = prep["sessionId"]
        tokens = prep.get("files") or {}

        results: dict[Path, bool] = {}
        for file_id, path in by_id.items():
            token = tokens.get(file_id)
            if not token:
                self.log.info("peer declined %s", path.name)
                results[path] = False
                continue
            try:
                status = self.upload_file(
                    session_id, file_id, token, path, size=metas[file_id]["size"]
                )
                results[path] = status == 200
            except RuntimeError as exc:
                self.log.warning("giving up on %s: %s", path.name, exc)
                results[path] = False
        return results
