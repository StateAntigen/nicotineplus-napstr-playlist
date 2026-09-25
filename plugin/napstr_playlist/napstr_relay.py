# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""A minimal Nostr relay client built only on the standard library.

Nicotine+ bundles a frozen Python runtime without ``websockets`` or
``websocket-client``, so this module implements just enough of RFC 6455 to
talk to relays: the HTTP upgrade handshake, masked client text frames, and
unmasking of server frames with ping/pong and continuation handling.

Both :meth:`RelayPool.publish` and :meth:`RelayPool.query` block, so call them
from a worker thread when running inside Nicotine+.
"""

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.parse

__all__ = [
    "DEFAULT_RELAYS",
    "RelayError",
    "RelayPool",
    "RelayResult",
    "WebSocketConnection",
]

DEFAULT_RELAYS = [
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.nostr.com",
    "wss://relay.primal.net",
    "wss://relay.snort.social",
    "wss://nostr.mom",
    "wss://relay.nostr.band",
]

_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_FRAME_PAYLOAD = 4 * 1024 * 1024
_OPCODE_CONTINUATION = 0x0
_OPCODE_TEXT = 0x1
_OPCODE_BINARY = 0x2
_OPCODE_CLOSE = 0x8
_OPCODE_PING = 0x9
_OPCODE_PONG = 0xA


class RelayError(Exception):
    """Raised when a relay could not be reached or rejected a request."""


class RelayResult:
    """Outcome of publishing one event to one relay."""

    __slots__ = ("relay_url", "accepted", "message", "error")

    def __init__(self, relay_url, accepted=False, message="", error=None):
        self.relay_url = relay_url
        self.accepted = accepted
        self.message = message or ""
        self.error = error

    @property
    def ok(self):
        return self.accepted and not self.error

    def describe(self):
        if self.error:
            return f"error: {self.error}"

        if self.accepted:
            return self.message or "accepted"

        return self.message or "rejected"

    def __repr__(self):
        return f"<RelayResult {self.relay_url} accepted={self.accepted} error={self.error}>"


def normalize_relay_url(url):
    """Return a relay URL with a scheme and no trailing slash, or ``None``."""

    if not url:
        return None

    url = str(url).strip()

    if not url:
        return None

    if not url.startswith(("ws://", "wss://", "http://", "https://")):
        url = f"wss://{url}"

    if url.startswith("http://"):
        url = "ws://" + url[len("http://"):]

    elif url.startswith("https://"):
        url = "wss://" + url[len("https://"):]

    return url.rstrip("/")


class WebSocketConnection:
    """A blocking WebSocket client for the ``ws``/``wss`` schemes."""

    def __init__(self, url, timeout=20.0, insecure=False, user_agent="napstr-playlist"):
        self.url = normalize_relay_url(url) or url
        self.timeout = timeout
        self.insecure = insecure
        self.user_agent = user_agent
        self._socket = None
        self._buffer = b""

    # -- lifecycle ---------------------------------------------------------

    def connect(self):

        parsed = urllib.parse.urlsplit(self.url)
        scheme = parsed.scheme.lower()

        if scheme not in {"ws", "wss"}:
            raise RelayError(f"unsupported relay scheme: {parsed.scheme!r}")

        host = parsed.hostname

        if not host:
            raise RelayError(f"relay URL has no host: {self.url!r}")

        port = parsed.port or (443 if scheme == "wss" else 80)
        resource = parsed.path or "/"

        if parsed.query:
            resource = f"{resource}?{parsed.query}"

        try:
            raw_socket = socket.create_connection((host, port), timeout=self.timeout)

        except OSError as error:
            raise RelayError(f"connection to {host}:{port} failed: {error}") from error

        if scheme == "wss":
            context = self._create_ssl_context()

            try:
                raw_socket = context.wrap_socket(raw_socket, server_hostname=host)

            except (ssl.SSLError, OSError) as error:
                raw_socket.close()
                raise RelayError(f"TLS handshake with {host} failed: {error}") from error

        raw_socket.settimeout(self.timeout)
        self._socket = raw_socket
        self._buffer = b""

        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {resource} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Origin: https://{host}\r\n"
            f"User-Agent: {self.user_agent}\r\n"
            "\r\n"
        )

        try:
            self._socket.sendall(request.encode("ascii"))
            status, headers = self._read_handshake_response()

        except (OSError, RelayError):
            self.close()
            raise

        if status != 101:
            self.close()
            raise RelayError(f"relay refused the upgrade with HTTP {status}")

        expected = base64.b64encode(
            hashlib.sha1(f"{key}{_WEBSOCKET_GUID}".encode()).digest()).decode()

        if headers.get("sec-websocket-accept") != expected:
            self.close()
            raise RelayError("relay returned an invalid Sec-WebSocket-Accept header")

        return self

    def _create_ssl_context(self):

        if self.insecure:
            context = ssl._create_unverified_context()  # pylint: disable=protected-access

        else:
            context = ssl.create_default_context()

        return context

    def _read_handshake_response(self):

        deadline = time.monotonic() + self.timeout

        while b"\r\n\r\n" not in self._buffer:
            if time.monotonic() > deadline:
                raise RelayError("timed out during the WebSocket handshake")

            chunk = self._socket.recv(4096)

            if not chunk:
                raise RelayError("relay closed the connection during the handshake")

            self._buffer += chunk

        header_block, self._buffer = self._buffer.split(b"\r\n\r\n", 1)
        lines = header_block.decode("latin-1").split("\r\n")
        status_parts = lines[0].split(" ", 2)

        try:
            status = int(status_parts[1])

        except (IndexError, ValueError):
            raise RelayError(f"malformed HTTP status line: {lines[0]!r}") from None

        headers = {}

        for line in lines[1:]:
            name, separator, value = line.partition(":")

            if separator:
                headers[name.strip().lower()] = value.strip()

        return status, headers

    def close(self):

        if self._socket is None:
            return

        try:
            if self._socket.fileno() != -1:
                self._send_frame(_OPCODE_CLOSE, b"")

        except OSError:
            pass

        try:
            self._socket.close()

        except OSError:
            pass

        self._socket = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *_exception):
        self.close()
        return False

    # -- framing -----------------------------------------------------------

    def _recv_exact(self, count):

        while len(self._buffer) < count:
            chunk = self._socket.recv(max(count - len(self._buffer), 4096))

            if not chunk:
                raise RelayError("relay closed the connection")

            self._buffer += chunk

        data, self._buffer = self._buffer[:count], self._buffer[count:]

        return data

    def _send_frame(self, opcode, payload):

        if len(payload) > _MAX_FRAME_PAYLOAD:
            raise RelayError("refusing to send an oversized WebSocket frame")

        header = bytearray()
        header.append(0x80 | opcode)

        mask_key = os.urandom(4)
        length = len(payload)

        if length < 126:
            header.append(0x80 | length)

        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)

        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)

        header += mask_key
        masked = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))

        self._socket.sendall(bytes(header) + masked)

    def send_text(self, text):
        self._send_frame(_OPCODE_TEXT, text.encode("utf-8"))

    def recv_text(self):
        """Return the next text message, or ``None`` when the relay closes."""

        fragments = []

        while True:
            first, second = self._recv_exact(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F

            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]

            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]

            if length > _MAX_FRAME_PAYLOAD:
                raise RelayError("relay sent an oversized WebSocket frame")

            mask_key = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length) if length else b""

            if mask_key:
                payload = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))

            if opcode == _OPCODE_CLOSE:
                return None

            if opcode == _OPCODE_PING:
                self._send_frame(_OPCODE_PONG, payload)
                continue

            if opcode == _OPCODE_PONG:
                continue

            if opcode in {_OPCODE_TEXT, _OPCODE_BINARY, _OPCODE_CONTINUATION}:
                fragments.append(payload)

                if not fin:
                    continue

                if opcode == _OPCODE_BINARY:
                    return bytes(b"".join(fragments))

                return b"".join(fragments).decode("utf-8", "replace")


class RelayPool:
    """Publishes events to a set of relays, one thread per relay."""

    def __init__(self, relays, timeout=20.0, insecure=False, user_agent="napstr-playlist"):
        self.relays = []
        self.timeout = timeout
        self.insecure = insecure
        self.user_agent = user_agent

        for relay in relays or []:
            normalized = normalize_relay_url(relay)

            if normalized and normalized not in self.relays:
                self.relays.append(normalized)

    def publish(self, event, timeout=None):
        """Publish ``event`` to every relay and return the results."""

        if not self.relays:
            raise RelayError("no relays configured")

        timeout = self.timeout if timeout is None else timeout
        results = [None] * len(self.relays)
        threads = []

        def worker(index, relay_url):
            results[index] = self._publish_to_relay(relay_url, event, timeout)

        for index, relay_url in enumerate(self.relays):
            thread = threading.Thread(
                target=worker, args=(index, relay_url),
                name=f"napstr-publish-{index}", daemon=True)
            threads.append(thread)
            thread.start()

        deadline = time.monotonic() + timeout + 1.0

        for thread in threads:
            remaining = max(deadline - time.monotonic(), 0.1)
            thread.join(timeout=remaining)

        return [
            result if result is not None else RelayResult(self.relays[index], error="timed out")
            for index, result in enumerate(results)
        ]

    def _publish_to_relay(self, relay_url, event, timeout):

        connection = WebSocketConnection(
            relay_url, timeout=timeout, insecure=self.insecure, user_agent=self.user_agent)

        try:
            connection.connect()
            connection.send_text(json.dumps(["EVENT", event], separators=(",", ":")))
            deadline = time.monotonic() + timeout

            while time.monotonic() < deadline:
                message = connection.recv_text()

                if message is None:
                    return RelayResult(relay_url, error="relay closed the connection")

                if isinstance(message, bytes):
                    continue

                try:
                    parsed = json.loads(message)

                except ValueError:
                    continue

                if not isinstance(parsed, list) or not parsed:
                    continue

                if parsed[0] == "OK" and len(parsed) >= 3 and parsed[1] == event.get("id"):
                    accepted = bool(parsed[2])
                    message_text = str(parsed[3]) if len(parsed) > 3 else ""

                    return RelayResult(relay_url, accepted=accepted, message=message_text)

                if parsed[0] == "NOTICE" and len(parsed) >= 2:
                    return RelayResult(relay_url, error=str(parsed[1]))

            return RelayResult(relay_url, error="timed out waiting for the OK response")

        except (RelayError, OSError, ValueError) as error:
            return RelayResult(relay_url, error=str(error))

        finally:
            connection.close()

    def query(self, filters, timeout=None, limit_events=500):
        """Fetch events matching ``filters`` from every relay.

        Returns ``(events, results)`` where ``events`` is de-duplicated by id.
        """

        if not self.relays:
            raise RelayError("no relays configured")

        timeout = self.timeout if timeout is None else timeout
        collected = {}
        results = [None] * len(self.relays)
        threads = []

        def worker(index, relay_url):
            events, result = self._query_relay(relay_url, filters, timeout, limit_events)
            results[index] = result

            for event in events:
                collected[event.get("id", "")] = event

        for index, relay_url in enumerate(self.relays):
            thread = threading.Thread(
                target=worker, args=(index, relay_url),
                name=f"napstr-query-{index}", daemon=True)
            threads.append(thread)
            thread.start()

        deadline = time.monotonic() + timeout + 1.0

        for thread in threads:
            thread.join(timeout=max(deadline - time.monotonic(), 0.1))

        return list(collected.values()), [
            result if result is not None else RelayResult(self.relays[index], error="timed out")
            for index, result in enumerate(results)
        ]

    def _query_relay(self, relay_url, filters, timeout, limit_events):

        connection = WebSocketConnection(
            relay_url, timeout=timeout, insecure=self.insecure, user_agent=self.user_agent)

        subscription_id = f"napstr{int(time.time() * 1000) % 1000000}"
        events = []

        try:
            connection.connect()
            connection.send_text(json.dumps(
                ["REQ", subscription_id] + list(filters), separators=(",", ":")))

            deadline = time.monotonic() + timeout

            while time.monotonic() < deadline:
                message = connection.recv_text()

                if message is None:
                    return events, RelayResult(relay_url, error="relay closed the connection")

                if isinstance(message, bytes):
                    continue

                try:
                    parsed = json.loads(message)

                except ValueError:
                    continue

                if not isinstance(parsed, list) or not parsed:
                    continue

                if parsed[0] == "EVENT" and len(parsed) >= 3:
                    events.append(parsed[2])

                    if len(events) >= limit_events:
                        break

                    continue

                if parsed[0] == "EOSE":
                    try:
                        connection.send_text(json.dumps(["CLOSE", subscription_id]))

                    except OSError:
                        pass

                    break

                if parsed[0] == "NOTICE" and len(parsed) >= 2:
                    return events, RelayResult(relay_url, error=str(parsed[1]))

            return events, RelayResult(relay_url, accepted=True, message=f"{len(events)} event(s)")

        except (RelayError, OSError, ValueError) as error:
            return events, RelayResult(relay_url, error=str(error))

        finally:
            connection.close()
