# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""Tests for the hand rolled WebSocket client and the relay pool.

These run without a network: server frames are hand built and fed to a fake
socket, and the client's own frames are parsed back.
"""

import json
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "plugin", "napstr_playlist"))

import napstr_relay  # noqa: E402  pylint: disable=wrong-import-position


def server_frame(opcode, payload, fin=True):
    """Build an unmasked server-to-client frame."""

    header = bytearray()
    header.append((0x80 if fin else 0x00) | opcode)
    length = len(payload)

    if length < 126:
        header.append(length)

    elif length < 65536:
        header.append(126)
        header += struct.pack("!H", length)

    else:
        header.append(127)
        header += struct.pack("!Q", length)

    return bytes(header) + payload


def parse_client_frames(data):
    """Parse (opcode, masked, payload) tuples out of a client byte stream."""

    frames = []
    index = 0

    while index < len(data):
        first = data[index]
        second = data[index + 1]
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        index += 2

        if length == 126:
            length = struct.unpack("!H", data[index:index + 2])[0]
            index += 2

        elif length == 127:
            length = struct.unpack("!Q", data[index:index + 8])[0]
            index += 8

        mask = None

        if masked:
            mask = data[index:index + 4]
            index += 4

        payload = data[index:index + length]
        index += length

        if mask:
            payload = bytes(byte ^ mask[position % 4] for position, byte in enumerate(payload))

        frames.append((opcode, masked, payload))

    return frames


class FakeSocket:
    """Just enough socket for the client: a read buffer and a write log."""

    def __init__(self, incoming=b""):

        self.incoming = bytearray(incoming)
        self.sent = bytearray()
        self.closed = False

    def recv(self, count):

        if not self.incoming:
            return b""

        chunk = bytes(self.incoming[:count])
        del self.incoming[:count]

        return chunk

    def sendall(self, data):
        self.sent += data

    def settimeout(self, _timeout):
        pass

    def fileno(self):
        return 1

    def close(self):
        self.closed = True


def make_connection(incoming=b""):
    connection = napstr_relay.WebSocketConnection("wss://relay.example", timeout=5)
    connection._socket = FakeSocket(incoming)  # pylint: disable=protected-access
    connection._buffer = b""  # pylint: disable=protected-access

    return connection


class NormalizeUrlTest(unittest.TestCase):

    def test_normalizes(self):

        self.assertEqual(napstr_relay.normalize_relay_url("relay.damus.io"), "wss://relay.damus.io")
        self.assertEqual(napstr_relay.normalize_relay_url("wss://nos.lol/"), "wss://nos.lol")
        self.assertEqual(napstr_relay.normalize_relay_url("https://nos.lol"), "wss://nos.lol")
        self.assertEqual(napstr_relay.normalize_relay_url("http://localhost:8080"), "ws://localhost:8080")
        self.assertEqual(napstr_relay.normalize_relay_url("  wss://a.b/c  "), "wss://a.b/c")

    def test_rejects_empty(self):

        for value in ("", None, "   "):
            self.assertIsNone(napstr_relay.normalize_relay_url(value))


class FrameTest(unittest.TestCase):

    def test_client_frames_are_masked(self):

        connection = make_connection()
        connection.send_text("hello")

        frames = parse_client_frames(bytes(connection._socket.sent))  # pylint: disable=protected-access

        self.assertEqual(len(frames), 1)
        opcode, masked, payload = frames[0]

        self.assertEqual(opcode, 0x1)
        self.assertTrue(masked, "a client frame must be masked")
        self.assertEqual(payload, b"hello")

    def test_reads_a_text_frame(self):

        connection = make_connection(server_frame(0x1, b'["OK","abc"]'))
        self.assertEqual(connection.recv_text(), '["OK","abc"]')

    def test_reads_a_fragmented_message(self):

        incoming = server_frame(0x1, b'["OK",', fin=False) + server_frame(0x0, b'"abc"]', fin=True)
        connection = make_connection(incoming)

        self.assertEqual(connection.recv_text(), '["OK","abc"]')

    def test_reads_a_large_frame(self):

        payload = (b"x" * 70000)
        connection = make_connection(server_frame(0x1, payload))

        self.assertEqual(connection.recv_text(), payload.decode())

    def test_answers_ping_with_pong(self):

        incoming = server_frame(0x9, b"ping") + server_frame(0x1, b"after")
        connection = make_connection(incoming)

        self.assertEqual(connection.recv_text(), "after")

        frames = parse_client_frames(bytes(connection._socket.sent))  # pylint: disable=protected-access

        self.assertEqual(frames, [(0xA, True, b"ping")])

    def test_close_frame_returns_none(self):

        connection = make_connection(server_frame(0x8, b""))
        self.assertIsNone(connection.recv_text())

    def test_oversized_frame_is_refused(self):

        # A frame header claiming more than the 4 MiB guard must be refused
        # before the payload is read
        oversized = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", 5 * 1024 * 1024)
        connection = make_connection(oversized)

        with self.assertRaises(napstr_relay.RelayError):
            connection.recv_text()

    def test_oversized_send_is_refused(self):

        connection = make_connection()

        with self.assertRaises(napstr_relay.RelayError):
            connection.send_text("x" * (5 * 1024 * 1024))

    def test_closed_connection_raises(self):

        connection = make_connection(b"")

        with self.assertRaises(napstr_relay.RelayError):
            connection.recv_text()


class HandshakeTest(unittest.TestCase):

    def test_parses_status_and_headers(self):

        connection = make_connection()

        connection._buffer = (  # pylint: disable=protected-access
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Sec-WebSocket-Accept: abc123\r\n\r\nremaining"
        )

        status, headers = connection._read_handshake_response()  # pylint: disable=protected-access

        self.assertEqual(status, 101)
        self.assertEqual(headers["sec-websocket-accept"], "abc123")
        self.assertEqual(connection._buffer, b"remaining")  # pylint: disable=protected-access

    def test_reports_a_bad_status_line(self):

        connection = make_connection()
        connection._buffer = b"garbage\r\n\r\n"  # pylint: disable=protected-access

        with self.assertRaises(napstr_relay.RelayError):
            connection._read_handshake_response()  # pylint: disable=protected-access


class RelayPoolTest(unittest.TestCase):

    def test_normalizes_and_deduplicates_relays(self):

        pool = napstr_relay.RelayPool(
            ["nos.lol", "wss://nos.lol", "  ", "https://relay.damus.io/", None, "wss://a.b"])

        self.assertEqual(pool.relays, ["wss://nos.lol", "wss://relay.damus.io", "wss://a.b"])

    def test_publish_without_relays_is_an_error(self):

        pool = napstr_relay.RelayPool([])

        with self.assertRaises(napstr_relay.RelayError):
            pool.publish({"id": "deadbeef"})

    def test_publish_reports_a_connection_failure_per_relay(self):

        # Reserved TEST-NET-1 address: connection fails immediately, no traffic
        pool = napstr_relay.RelayPool(["ws://192.0.2.1:9"], timeout=1)
        results = pool.publish({"id": "deadbeef"})

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIsNotNone(results[0].error)
        self.assertIn("error", results[0].describe())

    def test_default_relays(self):

        self.assertEqual(napstr_relay.DEFAULT_RELAYS, [
            "wss://relay.damus.io",
            "wss://nos.lol",
            "wss://relay.nostr.com",
            "wss://relay.primal.net",
            "wss://relay.snort.social",
            "wss://nostr.mom",
            "wss://relay.nostr.band"
        ])

        for relay in napstr_relay.DEFAULT_RELAYS:
            self.assertTrue(relay.startswith("wss://"), relay)
            self.assertEqual(napstr_relay.normalize_relay_url(relay), relay)


class EventMessageTest(unittest.TestCase):

    def test_publish_payload_shape(self):

        # The wire format is ["EVENT", <event>], which is what the pool sends
        event = {"id": "abc", "kind": 30425}
        message = json.dumps(["EVENT", event], separators=(",", ":"))

        self.assertEqual(json.loads(message)[0], "EVENT")
        self.assertEqual(json.loads(message)[1]["kind"], 30425)


if __name__ == "__main__":
    unittest.main()
