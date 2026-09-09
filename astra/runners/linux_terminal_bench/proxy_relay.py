from __future__ import annotations

import select
import socket
import socketserver
import threading
from dataclasses import dataclass
from urllib.parse import urlparse


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class _ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False


def _handler(upstream: tuple[str, int]):
    class RelayHandler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            try:
                remote = socket.create_connection(upstream, timeout=10)
            except OSError:
                return
            with remote:
                peers = {self.request: remote, remote: self.request}
                while True:
                    try:
                        readable, _, _ = select.select(peers, [], [], 1)
                    except OSError:
                        return
                    for source in readable:
                        try:
                            payload = source.recv(65536)
                            if not payload:
                                return
                            peers[source].sendall(payload)
                        except OSError:
                            return

    return RelayHandler


@dataclass
class ProxyRelay:
    proxy_url: str
    _server: _ThreadingTCPServer
    _thread: threading.Thread

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def start_proxy_relay(proxy_url: str, listen_host: str) -> ProxyRelay | None:
    parsed = urlparse(proxy_url)
    if parsed.hostname not in LOOPBACK_HOSTS:
        return None
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError("verifier proxy relay requires an HTTP proxy URL")
    upstream_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    server = _ThreadingTCPServer(
        (listen_host, 0),
        _handler((parsed.hostname, upstream_port)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    userinfo = (
        parsed.netloc.rsplit("@", 1)[0] + "@"
        if "@" in parsed.netloc
        else ""
    )
    relay_port = server.server_address[1]
    container_url = parsed._replace(
        netloc=f"{userinfo}host.docker.internal:{relay_port}"
    ).geturl()
    return ProxyRelay(container_url, server, thread)
