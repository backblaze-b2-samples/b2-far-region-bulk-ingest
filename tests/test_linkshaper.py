"""The link shaper: bandwidth cap, latency, injected drops and the CONNECT allow-list."""

import socket
import threading
import time

import pytest

from app.linkshaper import LinkShaper


@pytest.fixture()
def echo_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def echo(c):
        with c:
            while data := c.recv(65536):
                c.sendall(data)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=echo, args=(c,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    yield srv.getsockname()
    srv.close()


def _roundtrip(port, payload):
    with socket.create_connection(("127.0.0.1", port), timeout=10) as c:
        c.sendall(payload)
        c.shutdown(socket.SHUT_WR)
        got = b""
        try:
            while data := c.recv(65536):
                got += data
        except ConnectionResetError:
            pass
    return got


def test_forward_passes_bytes_and_caps_bandwidth(echo_server):
    with LinkShaper("forward", upstream=echo_server, bandwidth_bps=256 * 1024, latency_ms=20) as sh:
        start = time.monotonic()
        assert _roundtrip(sh.port, b"x" * (512 * 1024)) == b"x" * (512 * 1024)
        # 1 MiB crosses the shaper (both directions) under a 256 KiB/s cap.
        assert time.monotonic() - start > 2.0


def test_drop_cuts_the_connection_then_recovers(echo_server):
    with LinkShaper("forward", upstream=echo_server, drop_after_bytes=1024, max_drops=1) as sh:
        assert len(_roundtrip(sh.port, b"y" * 8192)) < 8192
        assert sh.stats.drops == 1
        assert _roundtrip(sh.port, b"z" * 8192) == b"z" * 8192


def test_connect_mode_refuses_hosts_outside_the_allow_list():
    with LinkShaper("connect", allowed_hosts=("s3.test-region-000.backblazeb2.com",)) as sh:
        with socket.create_connection(("127.0.0.1", sh.port), timeout=5) as c:
            c.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
            assert c.recv(100).startswith(b"HTTP/1.1 403")
        assert sh.stats.refused == 1
