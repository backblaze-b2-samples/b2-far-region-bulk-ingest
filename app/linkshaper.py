"""A small local link shaper: latency, a bandwidth cap and injected connection drops.

It runs on 127.0.0.1 in one of two modes:

- ``connect``: an HTTP CONNECT proxy. rclone reaches the real B2 endpoint through it
  (``HTTPS_PROXY``), so TLS stays end to end and certificates are still verified.
  Only the hosts in ``allowed_hosts`` on port 443 are tunnelled.
- ``forward``: a plain TCP forwarder to one upstream (the local S3 emulator in tests).

Shaping applies to every byte in both directions. It simulates a long or unstable
link on one machine; it is not a network emulator and makes no speed claim.
"""

from __future__ import annotations

import re
import socket
import threading
import time
from dataclasses import dataclass, field

_CHUNK = 64 * 1024
_UA = re.compile(rb"\r\nuser-agent:[ \t]*([^\r\n]*)", re.I)


class _TokenBucket:
    """Shared bandwidth cap across all connections (bytes per second)."""

    def __init__(self, rate: int):
        self.rate = rate
        self.tokens = float(rate)
        self.stamp = time.monotonic()
        self.lock = threading.Lock()

    def consume(self, n: int) -> None:
        if self.rate <= 0:
            return
        while n > 0:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(float(self.rate), self.tokens + (now - self.stamp) * self.rate)
                self.stamp = now
                take = min(n, int(self.tokens))
                if take > 0:
                    self.tokens -= take
                    n -= take
                    continue
                wait = (min(n, self.rate) - self.tokens) / self.rate
            time.sleep(max(wait, 0.001))


@dataclass
class ShaperStats:
    connections: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    drops: int = 0
    refused: int = 0
    user_agents: list[str] = field(default_factory=list)


class LinkShaper:
    def __init__(
        self,
        mode: str,
        *,
        upstream: tuple[str, int] | None = None,
        allowed_hosts: tuple[str, ...] = (),
        latency_ms: int = 0,
        bandwidth_bps: int = 0,
        drop_after_bytes: int = 0,
        max_drops: int = 0,
        record_user_agents: bool = False,
    ):
        if mode not in ("connect", "forward"):
            raise ValueError("mode must be 'connect' or 'forward'")
        if mode == "forward" and upstream is None:
            raise ValueError("forward mode needs an upstream (host, port)")
        if mode == "connect" and not allowed_hosts:
            raise ValueError("connect mode needs at least one allowed host")
        self.mode = mode
        self.upstream = upstream
        self.allowed_hosts = tuple(h.lower() for h in allowed_hosts)
        self.latency = latency_ms / 1000.0
        self.bucket = _TokenBucket(bandwidth_bps)
        self.drop_after = drop_after_bytes
        self.max_drops = max_drops
        self.record_user_agents = record_user_agents
        self.stats = ShaperStats()
        self._lock = threading.Lock()
        self._server: socket.socket | None = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> int:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(64)
        srv.settimeout(0.2)
        self._server = srv
        threading.Thread(target=self._accept_loop, daemon=True).start()
        return srv.getsockname()[1]

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.getsockname()[1]

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.close()

    def __enter__(self) -> "LinkShaper":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- connection handling -----------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()  # type: ignore[union-attr]
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            with self._lock:
                self.stats.connections += 1
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            upstream = self._open_upstream(client)
        except OSError:
            client.close()
            return
        if upstream is None:
            client.close()
            return
        state = {"up": 0, "closed": False}
        t = threading.Thread(target=self._pipe, args=(client, upstream, True, state), daemon=True)
        t.start()
        self._pipe(upstream, client, False, state)
        t.join(timeout=150)
        client.close()
        upstream.close()

    def _open_upstream(self, client: socket.socket) -> socket.socket | None:
        if self.mode == "forward":
            return socket.create_connection(self.upstream, timeout=30)  # type: ignore[arg-type]
        head = b""
        client.settimeout(30)
        while b"\r\n\r\n" not in head:
            data = client.recv(4096)
            if not data or len(head) > 16384:
                return None
            head += data
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        m = re.match(r"^CONNECT ([A-Za-z0-9.-]+):(\d+) HTTP/1\.[01]$", line)
        if not m or m.group(1).lower() not in self.allowed_hosts or m.group(2) != "443":
            with self._lock:
                self.stats.refused += 1
            client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return None
        upstream = socket.create_connection((m.group(1), 443), timeout=30)
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        rest = head.split(b"\r\n\r\n", 1)[1]
        if rest:
            upstream.sendall(rest)
        return upstream

    def _pipe(self, src: socket.socket, dst: socket.socket, upward: bool, state: dict) -> None:
        last = 0.0
        tail = b""
        src.settimeout(120)
        try:
            while True:
                data = src.recv(_CHUNK)
                if not data:
                    # Clean end of this direction: half-close, let the other direction finish.
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    return
                now = time.monotonic()
                if self.latency and now - last > 0.02:
                    time.sleep(self.latency)
                last = now
                if upward and self.record_user_agents:
                    buf = tail + data
                    with self._lock:
                        self.stats.user_agents.extend(
                            m.group(1).decode("latin-1").strip() for m in _UA.finditer(buf))
                    tail = data[-512:]
                self.bucket.consume(len(data))
                if upward:
                    state["up"] += len(data)
                    if self._should_drop(state):
                        break
                dst.sendall(data)
                with self._lock:
                    if upward:
                        self.stats.bytes_up += len(data)
                    else:
                        self.stats.bytes_down += len(data)
        except OSError:
            pass
        # A drop or an error tears down both directions at once.
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _should_drop(self, state: dict) -> bool:
        if not self.drop_after or state["up"] < self.drop_after:
            return False
        with self._lock:
            if self.stats.drops >= self.max_drops:
                return False
            self.stats.drops += 1
        return True
