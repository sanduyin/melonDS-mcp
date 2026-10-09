from __future__ import annotations

import socket
import threading
from typing import Callable

import pytest

from melonds_mcp.bridge_client import NativeBridgeClient
from melonds_mcp.bridge_protocol import BridgeFrame, FrameDecoder, MessageType, encode_frame
from melonds_mcp.errors import BridgeProtocolError, BridgeRemoteError


def _serve_once(
    stream: socket.socket,
    responder: Callable[[socket.socket, BridgeFrame], None],
) -> threading.Thread:
    def run() -> None:
        decoder = FrameDecoder()
        try:
            while True:
                chunk = stream.recv(4096)
                if not chunk:
                    return
                frames = decoder.feed(chunk)
                if frames:
                    responder(stream, frames[0])
                    return
        finally:
            stream.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_request_accepts_fragmented_event_then_binary_response() -> None:
    client_stream, server_stream = socket.socketpair()

    def respond(stream: socket.socket, request: BridgeFrame) -> None:
        event = encode_frame(
            MessageType.EVENT,
            0,
            {"event": "frame", "event_sequence": 4, "data": {"frame_number": "9"}},
        )
        response = encode_frame(
            MessageType.RESPONSE,
            request.request_id,
            {"ok": True, "result": {"format": "bgra8888"}},
            binary=b"pixels",
        )
        wire = event + response
        for offset in range(0, len(wire), 5):
            stream.sendall(wire[offset : offset + 5])

    thread = _serve_once(server_stream, respond)
    client = NativeBridgeClient(client_stream, receive_chunk_bytes=7)
    response = client.request("graphics.capture", {"screen": "top"})
    events = client.drain_events()

    assert response.result == {"format": "bgra8888"}
    assert response.binary == b"pixels"
    assert events[0].name == "frame"
    assert events[0].sequence == 4
    client.close()
    thread.join(timeout=2)


def test_structured_remote_error_preserves_code_and_details() -> None:
    client_stream, server_stream = socket.socketpair()

    def respond(stream: socket.socket, request: BridgeFrame) -> None:
        stream.sendall(
            encode_frame(
                MessageType.RESPONSE,
                request.request_id,
                {
                    "ok": False,
                    "error": {
                        "code": "STALE_STATE",
                        "message": "snapshot changed",
                        "details": {"current": "12"},
                    },
                },
            )
        )

    thread = _serve_once(server_stream, respond)
    client = NativeBridgeClient(client_stream)
    with pytest.raises(BridgeRemoteError) as caught:
        client.request("memory.write", {})
    assert caught.value.code == "STALE_STATE"
    assert caught.value.details == {"current": "12"}
    client.close()
    thread.join(timeout=2)


def test_mismatched_response_id_is_connection_fatal() -> None:
    client_stream, server_stream = socket.socketpair()

    def respond(stream: socket.socket, request: BridgeFrame) -> None:
        stream.sendall(
            encode_frame(
                MessageType.RESPONSE,
                request.request_id + 1,
                {"ok": True, "result": {}},
            )
        )

    thread = _serve_once(server_stream, respond)
    client = NativeBridgeClient(client_stream)
    with pytest.raises(BridgeProtocolError, match="does not match"):
        client.request("session.status")
    assert client.connected is False
    thread.join(timeout=2)


def test_cancel_can_be_sent_while_request_waits_for_response() -> None:
    client_stream, server_stream = socket.socketpair()
    request_seen = threading.Event()
    request_id: list[int] = []

    def server() -> None:
        decoder = FrameDecoder()
        try:
            while len(request_id) < 2:
                for frame in decoder.feed(server_stream.recv(4096)):
                    if frame.message_type == MessageType.REQUEST:
                        request_id.append(frame.request_id)
                        request_seen.set()
                    elif frame.message_type == MessageType.CANCEL:
                        assert frame.document["target_request_id"] == request_id[0]
                        request_id.append(frame.request_id)
                        server_stream.sendall(
                            encode_frame(
                                MessageType.RESPONSE,
                                request_id[0],
                                {
                                    "ok": False,
                                    "error": {
                                        "code": "CANCELLED",
                                        "message": "request cancelled",
                                    },
                                },
                            )
                        )
                        return
        finally:
            server_stream.close()

    server_thread = threading.Thread(target=server, daemon=True)
    server_thread.start()
    client = NativeBridgeClient(client_stream)
    caught: list[BaseException] = []

    def call() -> None:
        try:
            client.request("trace.wait")
        except BaseException as exc:
            caught.append(exc)

    request_thread = threading.Thread(target=call, daemon=True)
    request_thread.start()
    assert request_seen.wait(timeout=2)
    client.cancel(request_id[0])
    request_thread.join(timeout=2)

    assert request_thread.is_alive() is False
    assert isinstance(caught[0], BridgeRemoteError)
    assert caught[0].code == "CANCELLED"
    client.close()
    server_thread.join(timeout=2)
