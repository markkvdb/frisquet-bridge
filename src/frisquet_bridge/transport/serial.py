"""Async serial transport implementing the framed modem protocol.

pyserial is blocking, so a dedicated daemon reader thread does blocking
``readline`` calls and hands complete lines back to the event loop via
``loop.call_soon_threadsafe``. Writes are short and serialized behind a lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator

import serial
import structlog

from frisquet_bridge.frame import Frame, FrameError
from frisquet_bridge.logging import RawMessageRecorder
from frisquet_bridge.protocol import (
    ERR_RE,
    OK_RE,
    PONG_RE,
    READY_RE,
    format_cmd,
    parse_rx,
    verify_crc,
)
from frisquet_bridge.transport.base import ReceivedFrame, Transport, TransportError

log = structlog.get_logger(__name__)

_COMMAND_TIMEOUT = 3.0


class SerialTransport(Transport):
    def __init__(
        self,
        port: str,
        baud: int = 115200,
        *,
        command_timeout: float = _COMMAND_TIMEOUT,
        raw_recorder: RawMessageRecorder | None = None,
    ) -> None:
        self._port = port
        self._baud = baud
        self._command_timeout = command_timeout
        self._raw_recorder = raw_recorder

        self._serial: serial.Serial | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader: threading.Thread | None = None
        self._closing = False

        self._cmd_lock = asyncio.Lock()
        self._pending: asyncio.Future[None] | None = None
        self._subscribers: set[asyncio.Queue[ReceivedFrame]] = set()

    # -- lifecycle ---------------------------------------------------------

    async def open(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._serial = await self._loop.run_in_executor(None, self._open_serial)
        self._closing = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        log.info("serial_open", port=self._port, baud=self._baud)

    def _open_serial(self) -> serial.Serial:
        return serial.Serial(self._port, self._baud, timeout=0.2)

    async def close(self) -> None:
        self._closing = True
        if self._reader is not None:
            await asyncio.get_running_loop().run_in_executor(None, self._reader.join, 1.0)
            self._reader = None
        if self._serial is not None:
            self._serial.close()
            self._serial = None
        log.info("serial_closed", port=self._port)

    # -- reader thread -----------------------------------------------------

    def _read_loop(self) -> None:
        assert self._serial is not None
        assert self._loop is not None
        while not self._closing:
            try:
                raw = self._serial.readline()
            except (serial.SerialException, OSError):  # pragma: no cover
                log.exception("serial_read_failed")
                break
            if not raw:
                continue
            line = raw.decode("ascii", errors="replace").strip()
            if line:
                if self._raw_recorder is not None:
                    self._raw_recorder.record_line("rx", line)
                self._loop.call_soon_threadsafe(self._handle_line, line)

    def _handle_line(self, line: str) -> None:
        if line == "HB":
            return

        rx = parse_rx(line)
        if rx is not None:
            if not verify_crc(line):
                log.warning("rx_crc_mismatch", line=line)
                return
            self._dispatch_rx(rx.rssi, rx.frame_hex)
            return

        if OK_RE.match(line) or PONG_RE.match(line):
            log.debug("modem_ack", line=line)
            self._resolve_pending(None)
            return

        m = ERR_RE.match(line)
        if m:
            log.warning("modem_error", seq=m.group(1), reason=m.group(2))
            self._resolve_pending(TransportError(f"modem error: {m.group(2)}"))
            return

        if READY_RE.match(line):
            log.info("modem_ready", line=line)
            return

        log.debug("modem_line", line=line)

    def _dispatch_rx(self, rssi: int, frame_hex: str) -> None:
        try:
            raw = bytes.fromhex(frame_hex)
            frame = Frame.decode(raw)
        except (ValueError, FrameError) as exc:
            log.warning("rx_frame_decode_failed", frame_hex=frame_hex, error=str(exc))
            return
        if self._raw_recorder is not None:
            self._raw_recorder.record_frame("rx", frame, raw, rssi=rssi)
        log.debug(
            "rf_rx",
            rssi=rssi,
            to_addr=f"0x{frame.to_addr:02x}",
            from_addr=f"0x{frame.from_addr:02x}",
            association_id=f"0x{frame.association_id:02x}",
            request_id=f"0x{frame.request_id:02x}",
            msg_type=f"0x{frame.msg_type:02x}",
            control=f"0x{frame.control:02x}",
            payload_len=len(frame.payload),
        )
        received = ReceivedFrame(rssi=rssi, frame=frame, raw=raw)
        for queue in self._subscribers:
            queue.put_nowait(received)

    def _resolve_pending(self, error: Exception | None) -> None:
        fut = self._pending
        if fut is None or fut.done():
            return
        if error is None:
            fut.set_result(None)
        else:
            fut.set_exception(error)

    # -- commands ----------------------------------------------------------

    async def _command(self, line: str) -> None:
        if self._serial is None:
            raise TransportError("transport not open")
        loop = asyncio.get_running_loop()
        async with self._cmd_lock:
            fut: asyncio.Future[None] = loop.create_future()
            self._pending = fut
            try:
                formatted = format_cmd(line)
                if self._raw_recorder is not None:
                    self._raw_recorder.record_line("tx", formatted.strip())
                log.debug("modem_command", command=line.split(" ", 1)[0])
                await loop.run_in_executor(None, self._write, formatted)
                await asyncio.wait_for(fut, timeout=self._command_timeout)
            except TimeoutError as exc:
                log.warning("modem_command_timeout", command=line.split(" ", 1)[0], timeout=self._command_timeout)
                raise TransportError(f"timeout waiting for ack to {line!r}") from exc
            finally:
                self._pending = None

    def _write(self, text: str) -> None:
        assert self._serial is not None
        self._serial.write(text.encode("ascii"))
        self._serial.flush()

    async def set_network_id(self, network_id: bytes) -> None:
        if len(network_id) != 4:
            raise TransportError(f"network id must be 4 bytes, got {len(network_id)}")
        await self._command(f"NID {network_id.hex()}")

    async def send(self, frame: Frame) -> None:
        if self._raw_recorder is not None:
            self._raw_recorder.record_frame("tx", frame, frame.encode(), outcome="attempt")
        log.debug(
            "rf_tx",
            to_addr=f"0x{frame.to_addr:02x}",
            from_addr=f"0x{frame.from_addr:02x}",
            association_id=f"0x{frame.association_id:02x}",
            request_id=f"0x{frame.request_id:02x}",
            msg_type=f"0x{frame.msg_type:02x}",
            control=f"0x{frame.control:02x}",
            payload_len=len(frame.payload),
        )
        await self._command(f"TX {frame.encode().hex()}")

    async def listen(self) -> None:
        await self._command("LISTEN")

    async def sleep(self) -> None:
        await self._command("SLEEP")

    # -- receive -----------------------------------------------------------

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[ReceivedFrame]]:
        queue: asyncio.Queue[ReceivedFrame] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)
