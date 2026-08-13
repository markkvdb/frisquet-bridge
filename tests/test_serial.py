"""Unit tests for SerialTransport line handling (no hardware)."""

from __future__ import annotations

import asyncio

import pytest
import serial

from frisquet_bridge.frame import Frame
from frisquet_bridge.protocol import crc8, format_cmd
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
    transport._pending = (1, "OK", fut)

    transport._handle_line("OK 1")

    assert fut.done()
    assert fut.result() is None
    loop.close()


def test_handle_line_err_resolves_pending(transport: SerialTransport) -> None:
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    transport._pending = (1, "OK", fut)

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


async def test_reader_failure_fails_pending_command_and_signals_terminal_failure() -> None:
    class FailingSerial:
        def readline(self) -> bytes:
            raise serial.SerialException("device disconnected")

    transport = SerialTransport("/dev/null")
    transport._serial = FailingSerial()  # type: ignore[assignment]
    transport._loop = asyncio.get_running_loop()
    pending = asyncio.get_running_loop().create_future()
    transport._pending = (7, "OK", pending)

    transport._read_loop()
    with pytest.raises(TransportError, match="serial reader failed"):
        await pending
    with pytest.raises(TransportError, match="serial reader failed"):
        await transport.wait_failed()


async def test_intentional_close_does_not_signal_terminal_failure() -> None:
    class ClosingSerial:
        def readline(self) -> bytes:
            raise serial.SerialException("closed")

    transport = SerialTransport("/dev/null")
    transport._serial = ClosingSerial()  # type: ignore[assignment]
    transport._loop = asyncio.get_running_loop()
    transport._closing = True

    transport._read_loop()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(transport.wait_failed(), timeout=0.01)


async def test_set_network_id_wrong_length(transport: SerialTransport) -> None:
    with pytest.raises(TransportError, match="4 bytes"):
        await transport.set_network_id(b"\x01\x02")


async def test_command_when_not_open_raises(transport: SerialTransport) -> None:
    with pytest.raises(TransportError, match="not open"):
        await transport.listen()


def test_stale_and_wrong_kind_replies_do_not_resolve_pending(transport: SerialTransport) -> None:
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    transport._pending = (42, "OK", fut)

    transport._handle_line("OK 41")
    transport._handle_line("ERR 41 delayed")
    transport._handle_line("PONG 42")

    assert not fut.done()
    transport._handle_line("OK 42")
    assert fut.result() is None
    loop.close()


def test_matching_pong_resolves_ping_but_ok_does_not(transport: SerialTransport) -> None:
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    transport._pending = (99, "PONG", fut)

    transport._handle_line("OK 99")
    assert not fut.done()
    transport._handle_line("PONG 99")

    assert fut.result() is None
    loop.close()


async def test_commands_allocate_uint32_sequences_and_wrap(transport: SerialTransport) -> None:
    loop = asyncio.get_running_loop()

    class RecordingSerial:
        def __init__(self) -> None:
            self.writes: list[str] = []

        def write(self, data: bytes) -> None:
            text = data.decode("ascii")
            self.writes.append(text)
            seq = int(text.split()[1].removeprefix("@"))
            loop.call_soon_threadsafe(transport._handle_line, f"OK {seq}")

        def flush(self) -> None:
            pass

    serial_port = RecordingSerial()
    transport._serial = serial_port  # type: ignore[assignment]
    transport._next_seq = 0xFFFFFFFF

    await transport.listen()
    await transport.sleep()

    assert serial_port.writes[0].startswith("LISTEN @4294967295 ")
    assert serial_port.writes[1].startswith("SLEEP @0 ")
    assert serial_port.writes[0] == format_cmd("LISTEN @4294967295")


async def test_ping_sends_sequence_and_waits_for_matching_pong(transport: SerialTransport) -> None:
    loop = asyncio.get_running_loop()

    class PongSerial:
        def write(self, data: bytes) -> None:
            seq = int(data.decode("ascii").split()[1].removeprefix("@"))
            loop.call_soon_threadsafe(transport._handle_line, f"PONG {seq}")

        def flush(self) -> None:
            pass

    transport._serial = PongSerial()  # type: ignore[assignment]
    transport._next_seq = 1234

    await transport.ping()


async def test_timeout_cleanup_does_not_clear_newer_pending(transport: SerialTransport) -> None:
    loop = asyncio.get_running_loop()
    old = loop.create_future()
    newer = loop.create_future()
    transport._pending = (8, "OK", newer)

    transport._clear_pending(7, "OK", old)

    assert transport._pending == (8, "OK", newer)
