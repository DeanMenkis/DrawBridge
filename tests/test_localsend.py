#!/usr/bin/env python3
"""Tests for Drawbridge's LocalSend v2 implementation.

Pure stdlib ``unittest``, no hardware, no network beyond loopback. Covers the
failure modes that were found the hard way against the real LocalSend app:
chunked uploads, interrupted and stalled transfers, same-name concurrency,
and single-session batching.

The large-file size is configurable:

    DRAWBRIDGE_TEST_LARGE_MB=5 python3 -m unittest discover tests
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import typing
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "drawbridge"))

import localsend

logging.getLogger("drawbridge").addHandler(logging.NullHandler())

LARGE_FILE_MB = int(os.environ.get("DRAWBRIDGE_TEST_LARGE_MB", "100"))


def write_random_file(path: Path, size_bytes: int, chunk_size: int = 1024 * 1024) -> str:
    """Write random bytes in bounded chunks, returning the sha256 hex."""
    hasher = hashlib.sha256()
    remaining = size_bytes
    with open(path, "wb") as fh:
        while remaining > 0:
            take = min(chunk_size, remaining)
            data = os.urandom(take)
            fh.write(data)
            hasher.update(data)
            remaining -= take
    return hasher.hexdigest()


def start_loopback_server(
    target_dir: Path,
    *,
    chunk_size: int = 65536,
    request_timeout: float = localsend.DEFAULT_TIMEOUT,
):
    info = localsend.make_info(
        "Test Mac", 0, device_model="Mac", device_type="desktop", fingerprint="test-mac"
    )
    server = localsend.LocalSendServer(
        info,
        target_dir,
        bind_host="127.0.0.1",
        port=0,
        chunk_size=chunk_size,
        enable_multicast=False,
        request_timeout=request_timeout,
        logger=logging.getLogger("drawbridge.test.server"),
    )
    server.start()
    return server


def make_loopback_client(server, *, chunk_size: int = 65536):
    info = localsend.make_info(
        "Test Peer", 0, device_model="generic", device_type="headless", fingerprint="test-peer"
    )
    return localsend.LocalSendClient(
        info,
        "127.0.0.1",
        server.actual_port,
        chunk_size=chunk_size,
        timeout=60,
        retries=2,
        backoff=0.1,
        logger=logging.getLogger("drawbridge.test.client"),
    )


class LoopbackCase(unittest.TestCase):
    """Shared setup: temp dir, loopback server, client."""

    server_kwargs: typing.ClassVar[dict] = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="drawbridge-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.target_dir = self.tmp / "target"
        self.server = start_loopback_server(self.target_dir, **self.server_kwargs)
        self.addCleanup(self.server.stop)
        self.client = make_loopback_client(self.server)


class ProtocolBasicsTests(LoopbackCase):
    def test_register_returns_peer_info(self):
        info = self.client.register()
        self.assertEqual(info["fingerprint"], "test-mac")

    def test_prepare_upload_returns_session_and_token(self):
        resp = self.client.prepare_upload(
            {"f1": {"id": "f1", "fileName": "a.txt", "size": 3, "fileType": "text"}}
        )
        self.assertIn("sessionId", resp)
        self.assertIn("f1", resp["files"])

    def test_prepare_upload_empty_files_is_204(self):
        self.assertIsNone(self.client.prepare_upload({}))

    def test_small_file_roundtrip(self):
        src = self.tmp / "small.bin"
        expected = write_random_file(src, 12345)
        self.assertTrue(self.client.send_file(src))
        self.assertEqual(localsend.sha256_file(self.target_dir / "small.bin"), expected)
        self.assertFalse((self.target_dir / "small.bin.part").exists())

    def test_safe_filename_strips_path_components(self):
        self.assertEqual(localsend.safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(localsend.safe_filename(""), "upload.bin")
        self.assertEqual(localsend.safe_filename(".."), "upload.bin")

    def test_reserve_dest_is_atomic_under_contention(self):
        """Many threads reserving the same file name must each get a distinct
        destination; a check-then-open approach hands out duplicates and lets
        two uploads interleave into one corrupt file."""
        n = 40
        barrier = threading.Barrier(n)
        results = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            fd, _part, dest = localsend.reserve_dest(self.tmp, "clip.mp4")
            os.close(fd)
            with lock:
                results.append(dest)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(set(results)), n)


class LargeFileTests(LoopbackCase):
    def test_large_file_roundtrip_byte_for_byte(self):
        size = LARGE_FILE_MB * 1024 * 1024
        src = self.tmp / "movie.mp4"
        expected = write_random_file(src, size)
        self.assertTrue(self.client.send_file(src, compute_sha256=True))
        dest = self.target_dir / "movie.mp4"
        self.assertEqual(dest.stat().st_size, size)
        self.assertEqual(localsend.sha256_file(dest), expected)


class ChunkedUploadTests(LoopbackCase):
    """The official LocalSend app streams uploads with Transfer-Encoding:
    chunked and no Content-Length. Before this was supported, such uploads
    produced empty files."""

    def _prepare(self, filename: str, size: int):
        resp = self.client.prepare_upload(
            {"c1": {"id": "c1", "fileName": filename, "size": size, "fileType": "image"}}
        )
        return resp["sessionId"], resp["files"]["c1"]

    def test_chunked_upload_arrives_byte_for_byte(self):
        payload = os.urandom(3_000_000)
        session_id, token = self._prepare("IMG_0001.jpg", len(payload))
        sock = socket.create_connection(("127.0.0.1", self.server.actual_port), timeout=10)
        try:
            head = (
                f"POST /api/localsend/v2/upload?sessionId={session_id}&fileId=c1&token={token} HTTP/1.1\r\n"
                "Host: x\r\nTransfer-Encoding: chunked\r\n"
                "Content-Type: application/octet-stream\r\n\r\n"
            )
            sock.sendall(head.encode("ascii"))
            step = 128 * 1024
            for i in range(0, len(payload), step):
                part = payload[i : i + step]
                sock.sendall(f"{len(part):x}\r\n".encode("ascii") + part + b"\r\n")
            sock.sendall(b"0\r\n\r\n")
            status_line = sock.recv(4096).decode("ascii", "replace").split("\r\n")[0]
        finally:
            sock.close()
        self.assertIn("200", status_line)
        dest = self.target_dir / "IMG_0001.jpg"
        self.assertEqual(
            hashlib.sha256(dest.read_bytes()).hexdigest(),
            hashlib.sha256(payload).hexdigest(),
        )

    def test_truncated_chunked_upload_leaves_nothing(self):
        session_id, token = self._prepare("cut.jpg", 3_000_000)
        sock = socket.create_connection(("127.0.0.1", self.server.actual_port), timeout=10)
        try:
            head = (
                f"POST /api/localsend/v2/upload?sessionId={session_id}&fileId=c1&token={token} HTTP/1.1\r\n"
                "Host: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            )
            sock.sendall(head.encode("ascii"))
            part = os.urandom(65536)
            sock.sendall(f"{len(part):x}\r\n".encode("ascii") + part + b"\r\n")
            # close without the terminating 0-chunk
        finally:
            sock.close()
        time.sleep(0.5)
        self.assertFalse((self.target_dir / "cut.jpg").exists())
        self.assertFalse((self.target_dir / "cut.jpg.part").exists())


class InterruptedTests(LoopbackCase):
    def _send_partial_and_die(self, filename: str, declared: int, actual: bytes):
        resp = self.client.prepare_upload(
            {"i1": {"id": "i1", "fileName": filename, "size": declared, "fileType": "video"}}
        )
        session_id, token = resp["sessionId"], resp["files"]["i1"]
        sock = socket.create_connection(("127.0.0.1", self.server.actual_port), timeout=5)
        try:
            head = (
                f"POST /api/localsend/v2/upload?sessionId={session_id}&fileId=i1&token={token} HTTP/1.1\r\n"
                f"Host: x\r\nContent-Length: {declared}\r\nConnection: close\r\n\r\n"
            )
            sock.sendall(head.encode("ascii"))
            sock.sendall(actual)
        finally:
            sock.close()

    def test_interrupted_upload_leaves_no_corrupt_file(self):
        self._send_partial_and_die("interrupted.mp4", 5 * 1024 * 1024, os.urandom(1024 * 1024))
        time.sleep(0.5)
        self.assertEqual(
            list(self.target_dir.iterdir()) if self.target_dir.exists() else [], []
        )

    def test_server_still_works_after_an_interrupted_upload(self):
        self._send_partial_and_die("dropped.mp4", 2 * 1024 * 1024, os.urandom(1000))
        time.sleep(0.3)
        src = self.tmp / "good.bin"
        expected = write_random_file(src, 300_000)
        self.assertTrue(self.client.send_file(src))
        self.assertEqual(localsend.sha256_file(self.target_dir / "good.bin"), expected)


class StalledConnectionTests(LoopbackCase):
    """A half-open connection must time out, clean up, and not wedge the
    server; without a per-socket timeout it hangs a worker forever."""

    server_kwargs: typing.ClassVar[dict] = {"request_timeout": 1.0}

    def test_stalled_upload_times_out_and_server_recovers(self):
        declared = 5 * 1024 * 1024
        resp = self.client.prepare_upload(
            {"s1": {"id": "s1", "fileName": "stalled.mp4", "size": declared, "fileType": "video"}}
        )
        session_id, token = resp["sessionId"], resp["files"]["s1"]
        sock = socket.create_connection(("127.0.0.1", self.server.actual_port), timeout=5)
        try:
            head = (
                f"POST /api/localsend/v2/upload?sessionId={session_id}&fileId=s1&token={token} HTTP/1.1\r\n"
                f"Host: x\r\nContent-Length: {declared}\r\nConnection: close\r\n\r\n"
            )
            sock.sendall(head.encode("ascii"))
            sock.sendall(os.urandom(64 * 1024))
            time.sleep(2.0)  # go silent past request_timeout, without closing
        finally:
            sock.close()
        self.assertFalse((self.target_dir / "stalled.mp4").exists())
        self.assertFalse((self.target_dir / "stalled.mp4.part").exists())
        src = self.tmp / "after.bin"
        expected = write_random_file(src, 250_000)
        self.assertTrue(self.client.send_file(src))
        self.assertEqual(localsend.sha256_file(self.target_dir / "after.bin"), expected)


class ConcurrencyTests(LoopbackCase):
    def test_two_concurrent_uploads_both_complete_correctly(self):
        src_a = self.tmp / "clip_a.mp4"
        src_b = self.tmp / "clip_b.mp4"
        sha_a = write_random_file(src_a, 4 * 1024 * 1024)
        sha_b = write_random_file(src_b, 6 * 1024 * 1024)
        errors = []

        def send(path):
            try:
                make_loopback_client(self.server).send_file(path)
            except Exception as exc:  # noqa: BLE001 - surfaced via assertion
                errors.append(exc)

        threads = [threading.Thread(target=send, args=(p,)) for p in (src_a, src_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(localsend.sha256_file(self.target_dir / "clip_a.mp4"), sha_a)
        self.assertEqual(localsend.sha256_file(self.target_dir / "clip_b.mp4"), sha_b)

    def test_two_concurrent_same_name_uploads_do_not_corrupt(self):
        dir_a = self.tmp / "a"
        dir_b = self.tmp / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        sha_a = write_random_file(dir_a / "clip.mp4", 5 * 1024 * 1024)
        sha_b = write_random_file(dir_b / "clip.mp4", 7 * 1024 * 1024)
        errors = []

        def send(path):
            try:
                make_loopback_client(self.server).send_file(path, compute_sha256=True)
            except Exception as exc:  # noqa: BLE001 - surfaced via assertion
                errors.append(exc)

        threads = [
            threading.Thread(target=send, args=(p,))
            for p in (dir_a / "clip.mp4", dir_b / "clip.mp4")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        received = sorted(p.name for p in self.target_dir.iterdir())
        self.assertEqual(received, ["clip.1.mp4", "clip.mp4"])
        got = {localsend.sha256_file(self.target_dir / n) for n in received}
        self.assertEqual(got, {sha_a, sha_b})


class BatchTests(LoopbackCase):
    def test_batch_uses_one_session_and_delivers_all(self):
        calls = []
        orig = self.client.prepare_upload
        self.client.prepare_upload = lambda metas: (calls.append(len(metas)) or orig(metas))

        shas = {}
        files = []
        for name, size in (("a.jpg", 2_000_000), ("b video.mp4", 4_000_000), ("c.pdf", 300_000)):
            p = self.tmp / name
            shas[name] = write_random_file(p, size)
            files.append(p)

        results = self.client.send_files(files, compute_sha256=True)
        self.assertEqual(calls, [3], "batch must declare all files in one prepare-upload")
        self.assertTrue(all(results.values()))
        for name, sha in shas.items():
            self.assertEqual(localsend.sha256_file(self.target_dir / name), sha)

    def test_one_failed_file_does_not_sink_the_batch(self):
        good = self.tmp / "good.jpg"
        bad = self.tmp / "bad.bin"
        write_random_file(good, 500_000)
        bad.write_bytes(os.urandom(1000))

        orig = self.client.upload_file

        def flaky(session_id, file_id, token, path, *, size):
            if Path(path).name == "bad.bin":
                raise RuntimeError("simulated per-file failure")
            return orig(session_id, file_id, token, path, size=size)

        self.client.upload_file = flaky
        results = self.client.send_files([good, bad])
        self.assertTrue(results[good])
        self.assertFalse(results[bad])
        self.assertTrue((self.target_dir / "good.jpg").exists())


if __name__ == "__main__":
    unittest.main()
