"""Strict loopback WebSocket relay for Playwright-to-BitBrowser CDP.

Playwright's public ``connect_over_cdp`` API follows WebSocket redirects and
does not expose a switch to disable that behavior.  Resolving and preflighting a
loopback endpoint is not enough: a local endpoint could return ``101`` during
the preflight and a redirect during Playwright's second connection.

This relay is the connection Playwright actually opens.  It accepts one local
client, opens the already-validated loopback CDP socket itself, requires an
immediate RFC 6455 ``101`` response, and then copies opaque WebSocket bytes in
both directions.  No redirect response is ever forwarded to Playwright.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import re
import select
import socket
import ssl
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


_BROWSER_PATH = re.compile(r"/devtools/browser/[A-Za-z0-9._:-]{1,256}")
_MAX_HEADERS = 64 * 1024


class CdpRelayError(RuntimeError):
    """The strict relay could not establish a safe CDP WebSocket."""


def _read_headers(
    connection: socket.socket | ssl.SSLSocket,
) -> tuple[bytes, bytes]:
    payload = bytearray()
    while b"\r\n\r\n" not in payload and len(payload) < _MAX_HEADERS:
        chunk = connection.recv(4096)
        if not chunk:
            break
        payload.extend(chunk)
    marker = payload.find(b"\r\n\r\n")
    if marker < 0:
        raise CdpRelayError("WebSocket 握手头不完整")
    boundary = marker + 4
    return bytes(payload[:boundary]), bytes(payload[boundary:])


def _parse_headers(payload: bytes) -> tuple[bytes, dict[bytes, bytes]]:
    lines = payload[:-4].split(b"\r\n")
    if not lines:
        raise CdpRelayError("WebSocket 握手为空")
    headers: dict[bytes, bytes] = {}
    for line in lines[1:]:
        if b":" not in line:
            raise CdpRelayError("WebSocket 握手头格式无效")
        name, value = line.split(b":", 1)
        normalized = name.strip().lower()
        cleaned = value.strip()
        if normalized in headers:
            headers[normalized] += b", " + cleaned
        else:
            headers[normalized] = cleaned
    return lines[0], headers


def _header_has_token(value: bytes | None, token: bytes) -> bool:
    return value is not None and token in {
        item.strip().lower() for item in value.split(b",")
    }


@dataclass
class StrictLoopbackCdpRelay:
    upstream_endpoint: str
    timeout_seconds: float = 5.0
    _listener: socket.socket | None = field(init=False, default=None, repr=False)
    _client: socket.socket | None = field(init=False, default=None, repr=False)
    _upstream: socket.socket | ssl.SSLSocket | None = field(
        init=False, default=None, repr=False
    )
    _thread: threading.Thread | None = field(init=False, default=None, repr=False)
    _stop: threading.Event = field(
        init=False, default_factory=threading.Event, repr=False
    )
    _lock: threading.RLock = field(
        init=False, default_factory=threading.RLock, repr=False
    )
    _failure: str | None = field(init=False, default=None)
    _path: str = field(init=False, repr=False)
    _host: str = field(init=False, repr=False)
    _port: int = field(init=False, repr=False)
    _secure: bool = field(init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            parsed = urlparse(self.upstream_endpoint)
            port = parsed.port
            host = parsed.hostname
        except ValueError as exc:
            raise CdpRelayError("BitBrowser CDP 地址无效") from exc
        try:
            loopback = host is not None and ipaddress.ip_address(
                "127.0.0.1" if host == "localhost" else host
            ).is_loopback
        except ValueError:
            loopback = False
        if (
            parsed.scheme not in {"ws", "wss"}
            or not loopback
            or port is None
            or not 1 <= port <= 65535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not _BROWSER_PATH.fullmatch(parsed.path)
        ):
            raise CdpRelayError("BitBrowser CDP 必须是浏览器级本机 WebSocket")
        self._path = parsed.path
        self._host = "127.0.0.1" if host == "localhost" else (host or "127.0.0.1")
        self._port = port
        self._secure = parsed.scheme == "wss"

    @property
    def failure(self) -> str | None:
        with self._lock:
            return self._failure

    def start(self) -> str:
        with self._lock:
            if self._thread is not None:
                raise CdpRelayError("CDP relay 已启动")
            listener = self._create_listener()
            self._listener = listener
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="bitbrowser-cdp-relay",
                daemon=True,
            )
            self._thread.start()
            port = int(listener.getsockname()[1])
        return f"ws://127.0.0.1:{port}{self._path}"

    @staticmethod
    def _create_listener() -> socket.socket:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # This is a security boundary, not a restartable public server.
            # SO_REUSEADDR is unsafe for listeners on Windows because another
            # process can bind the same port and receive an indeterminate share
            # of connections.  Ask Winsock for exclusive ownership when exposed.
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                listener.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(0.2)
            return listener
        except OSError:
            listener.close()
            raise

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            sockets: tuple[Any, ...] = (
                self._listener,
                self._client,
                self._upstream,
            )
            thread = self._thread
        for connection in sockets:
            if connection is None:
                continue
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, min(5.0, self.timeout_seconds + 0.5)))
        with self._lock:
            self._listener = None
            self._client = None
            self._upstream = None
            self._thread = None

    def _run(self) -> None:
        try:
            client = self._accept_client()
            if client is None:
                return
            with self._lock:
                self._client = client
            client.settimeout(self.timeout_seconds)
            request, client_tail = _read_headers(client)
            request_line, request_headers = _parse_headers(request)
            self._validate_client_request(request_line, request_headers)
            upstream = self._connect_upstream()
            with self._lock:
                self._upstream = upstream
            upstream.settimeout(self.timeout_seconds)
            upstream.sendall(self._upstream_request(request_headers))
            response, upstream_tail = _read_headers(upstream)
            self._validate_upstream_response(response, request_headers)
            client.sendall(response)
            if client_tail:
                upstream.sendall(client_tail)
            if upstream_tail:
                client.sendall(upstream_tail)
            client.settimeout(None)
            upstream.settimeout(None)
            self._bridge(client, upstream)
        except (CdpRelayError, OSError, ssl.SSLError, TimeoutError) as exc:
            with self._lock:
                self._failure = str(exc)[:240]
        finally:
            self._stop.set()
            with self._lock:
                sockets: tuple[Any, ...] = (self._client, self._upstream)
            for connection in sockets:
                if connection is not None:
                    try:
                        connection.close()
                    except OSError:
                        pass

    def _accept_client(self) -> socket.socket | None:
        while not self._stop.is_set():
            with self._lock:
                listener = self._listener
            if listener is None:
                return None
            try:
                client, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return None
            if address[0] != "127.0.0.1":
                client.close()
                continue
            try:
                listener.close()
            except OSError:
                pass
            with self._lock:
                self._listener = None
            return client
        return None

    def _connect_upstream(self) -> socket.socket | ssl.SSLSocket:
        connection = socket.create_connection(
            (self._host, self._port), timeout=self.timeout_seconds
        )
        try:
            peer = str(connection.getpeername()[0])
            if not ipaddress.ip_address(peer).is_loopback:
                raise CdpRelayError("BitBrowser CDP 实际连接不是本机地址")
        except BaseException:
            connection.close()
            raise
        if not self._secure:
            return connection
        try:
            return ssl.create_default_context().wrap_socket(
                connection,
                server_hostname=self._host,
            )
        except BaseException:
            connection.close()
            raise

    def _validate_client_request(
        self, request_line: bytes, headers: dict[bytes, bytes]
    ) -> None:
        expected = f"GET {self._path} HTTP/1.1".encode("ascii")
        if request_line != expected:
            raise CdpRelayError("Playwright CDP 请求路径无效")
        if not _header_has_token(headers.get(b"upgrade"), b"websocket"):
            raise CdpRelayError("Playwright 未请求 WebSocket 升级")
        if not _header_has_token(headers.get(b"connection"), b"upgrade"):
            raise CdpRelayError("Playwright WebSocket Connection 头无效")
        key = headers.get(b"sec-websocket-key")
        try:
            decoded = base64.b64decode(key or b"", validate=True)
        except (ValueError, binascii.Error) as exc:
            raise CdpRelayError("Playwright WebSocket key 无效") from exc
        if len(decoded) != 16 or headers.get(b"sec-websocket-version") != b"13":
            raise CdpRelayError("Playwright WebSocket 版本或 key 无效")

    def _upstream_request(self, headers: dict[bytes, bytes]) -> bytes:
        host = (
            f"[{self._host}]:{self._port}"
            if ":" in self._host
            else f"{self._host}:{self._port}"
        )
        lines = [
            f"GET {self._path} HTTP/1.1".encode("ascii"),
            f"Host: {host}".encode("ascii"),
            b"Upgrade: websocket",
            b"Connection: Upgrade",
            b"Sec-WebSocket-Key: " + headers[b"sec-websocket-key"],
            b"Sec-WebSocket-Version: 13",
        ]
        for name in (
            b"origin",
            b"sec-websocket-protocol",
            b"sec-websocket-extensions",
            b"user-agent",
        ):
            value = headers.get(name)
            if value is not None:
                lines.append(name.title() + b": " + value)
        return b"\r\n".join(lines) + b"\r\n\r\n"

    def _validate_upstream_response(
        self, payload: bytes, request_headers: dict[bytes, bytes]
    ) -> None:
        status_line, headers = _parse_headers(payload)
        if not re.match(rb"^HTTP/1\.[01] 101(?: |$)", status_line):
            raise CdpRelayError("BitBrowser CDP 拒绝连接或尝试重定向")
        if not _header_has_token(headers.get(b"upgrade"), b"websocket"):
            raise CdpRelayError("BitBrowser CDP Upgrade 头无效")
        if not _header_has_token(headers.get(b"connection"), b"upgrade"):
            raise CdpRelayError("BitBrowser CDP Connection 头无效")
        key = request_headers[b"sec-websocket-key"]
        expected = base64.b64encode(
            hashlib.sha1(
                key
                + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            ).digest()
        )
        if headers.get(b"sec-websocket-accept") != expected:
            raise CdpRelayError("BitBrowser CDP WebSocket 握手校验失败")

    def _bridge(
        self,
        client: socket.socket,
        upstream: socket.socket | ssl.SSLSocket,
    ) -> None:
        peers: dict[Any, Any] = {client: upstream, upstream: client}
        while not self._stop.is_set():
            try:
                readable, _, _ = select.select(list(peers), [], [], 0.2)
            except (OSError, ValueError):
                return
            for source in readable:
                try:
                    chunk = source.recv(64 * 1024)
                except (OSError, ssl.SSLError):
                    return
                if not chunk:
                    return
                try:
                    peers[source].sendall(chunk)
                except (OSError, ssl.SSLError):
                    return
