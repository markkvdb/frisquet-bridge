"""Test doubles for frisquet-bridge."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from frisquet_bridge.connect.codec import encode_temp8
from frisquet_bridge.connect.ops import ZONE_MODE_OPTIONS_DEFAULT
from frisquet_bridge.frame import MSG_INIT, MSG_READ, Frame
from frisquet_bridge.model import BoilerData, ZoneMode, ZoneSchedule
from frisquet_bridge.transport.base import ReceivedFrame, Transport, TransportError

DEFAULT_ZONE_SCHEDULE_BYTES = bytes.fromhex(
    "00e0ffffffff"
    "00e0ffffff7f"
    "00e0ffffff7f"
    "00e0ffffff7f"
    "00e0ffffff7f"
    "00e0ffffff7f"
    "00e0ffffffff"
)


def make_zone_config_body(
    *,
    comfort: float = 20.0,
    reduced: float = 17.0,
    frost: float = 7.0,
    mode: ZoneMode = ZoneMode.AUTO,
    mode_options: int = ZONE_MODE_OPTIONS_DEFAULT,
) -> bytes:
    return bytes(
        (
            encode_temp8(comfort),
            encode_temp8(reduced),
            encode_temp8(frost),
            mode.byte,
            mode_options,
            0x00,
        )
    ) + DEFAULT_ZONE_SCHEDULE_BYTES


def seed_zone_metadata(data: BoilerData, zone: int = 1) -> None:
    zs = data.zones[zone]
    zs.schedule = ZoneSchedule.decode(DEFAULT_ZONE_SCHEDULE_BYTES)
    zs.comfort_temperature = 20.0
    zs.reduced_temperature = 17.0
    zs.frost_temperature = 7.0
    zs.mode_options = ZONE_MODE_OPTIONS_DEFAULT


class FakeTransport(Transport):
    """In-memory transport for protocol client tests."""

    def __init__(
        self,
        *,
        auto_reply: bool = True,
        send_raises: TransportError | None = None,
        read_responses: dict[int, bytes] | None = None,
        init_responses: dict[int, bytes] | None = None,
    ) -> None:
        self.network_ids: list[bytes] = []
        self.sent: list[Frame] = []
        self._subs: set[asyncio.Queue[ReceivedFrame]] = set()
        self.auto_reply = auto_reply
        self.send_raises = send_raises
        self.read_responses = read_responses or {}
        self.init_responses = init_responses or {}

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def set_network_id(self, network_id: bytes) -> None:
        if len(network_id) != 4:
            raise TransportError(f"network id must be 4 bytes, got {len(network_id)}")
        self.network_ids.append(network_id)

    async def listen(self) -> None:
        pass

    async def sleep(self) -> None:
        pass

    async def send(self, frame: Frame) -> None:
        if self.send_raises is not None:
            raise self.send_raises
        self.sent.append(frame)
        if self.auto_reply and frame.msg_type == MSG_READ:
            self._emit_read_response(frame)
        elif self.auto_reply and frame.msg_type == MSG_INIT:
            self._emit_init_response(frame)

    def _emit_read_response(self, request: Frame) -> None:
        addr = int.from_bytes(request.payload[0:2], "big")
        payload = self.read_responses.get(addr, bytes.fromhex("38024b0269"))
        resp = Frame(
            to_addr=request.from_addr,
            from_addr=request.to_addr,
            association_id=request.association_id,
            request_id=request.request_id,
            control=0x81,
            msg_type=request.msg_type,
            payload=payload,
        )
        self.push_frame(resp, rssi=-50)

    def _emit_init_response(self, request: Frame) -> None:
        control = request.control & 0x7F
        payload = self.init_responses.get(control, b"")
        if not payload:
            return
        resp = Frame(
            to_addr=request.from_addr,
            from_addr=request.to_addr,
            association_id=request.association_id,
            request_id=request.request_id,
            control=control | 0x80,
            msg_type=request.msg_type,
            payload=payload,
        )
        self.push_frame(resp, rssi=-50)

    def push_frame(self, frame: Frame, *, rssi: int = -60) -> None:
        raw = frame.encode()
        received = ReceivedFrame(rssi=rssi, frame=frame, raw=raw)
        for queue in self._subs:
            queue.put_nowait(received)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[ReceivedFrame]]:
        queue: asyncio.Queue[ReceivedFrame] = asyncio.Queue()
        self._subs.add(queue)
        try:
            yield queue
        finally:
            self._subs.discard(queue)
