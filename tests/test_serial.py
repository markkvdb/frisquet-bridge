"""Unit tests for SerialTransport line handling (no hardware)."""

from __future__ import annotations

import asyncio

import pytest

from frisquet_bridge.frame import Frame
from frisquet_bridge.protocol import crc8
from frisquet_bridge.transport.base import TransportError
from frisquet_bridge.transport.serial import SerialTransport


def _rx_line(rssi: int, frame_hex: str) -> str:
    body = f"RX {rssi} {frame_hex}"
    return f"{body} {crc8(body.encode('ascii')):02x}"


@pytest.fixture
def transport() -> SerialTransport:
    return SerialTransport("/dev/null")


def test_handle_line_dispatches_valid_rx(transport: SerialTransport) -> None:
    frame = Frame(
        to_addr=0x80,
        from_addr=0x7E,
        association_id=0x9C,
        request_id=0x18,
        control=0x01,
        msg_type=0x03,
        payload=bytes.fromhex("79e0001c"),
    )
    line = _rx_line(-55, frame.encode().hex())
    queue: asyncio.Queue = asyncio.Queue()
    transport._subscribers.add(queue)

    transport._handle_line(line)

    received = queue.get_nowait()
    assert received.rssi == -55
    assert received.frame == frame


def test_handle_line_ignores_bad_crc(transport: SerialTransport) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    transport._subscribers.add(queue)

    transport._handle_line("RX -55 deadbeef ff")

    assert queue.empty()


def test_handle_line_ignores_invalid_hex(transport: SerialTransport) -> None:
    body = "RX -55 nothex"
    line = f"{body} {crc8(body.encode('ascii')):02x}"
    queue: asyncio.Queue = asyncio.Queue()
    transport._subscribers.add(queue)

    transport._handle_line(line)

    assert queue.empty()


def test_handle_line_ok_resolves_pending(transport: SerialTransport) -> None:
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    transport._pending = fut

    transport._handle_line("OK 1")

    assert fut.done()
    assert fut.result() is None
    loop.close()


def test_handle_line_err_resolves_pending(transport: SerialTransport) -> None:
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    transport._pending = fut

    transport._handle_line("ERR 1 bad_crc")

    assert fut.done()
    with pytest.raises(TransportError, match="bad_crc"):
        fut.result()
    loop.close()


def test_handle_line_heartbeat_is_ignored(transport: SerialTransport) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    transport._subscribers.add(queue)

    transport._handle_line("HB")

    assert queue.empty()


async def test_set_network_id_wrong_length(transport: SerialTransport) -> None:
    with pytest.raises(TransportError, match="4 bytes"):
        await transport.set_network_id(b"\x01\x02")


async def test_command_when_not_open_raises(transport: SerialTransport) -> None:
    with pytest.raises(TransportError, match="not open"):
        await transport.listen()
