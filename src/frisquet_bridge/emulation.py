"""Passive Connect + virtual satellite emulation."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable

from frisquet_bridge.connect.decode import (
    ADDR_CONSUMPTION,
    ADDR_DAILY_CONSUMPTION,
    ADDR_DATE,
    ADDR_DHW_MODE,
    ADDR_SATELLITE_CONSIGNE,
    ADDR_SENSORS,
    ADDR_ZONE_CONFIG,
    decode_clock,
    decode_consumption,
    decode_daily_consumption,
    decode_dhw_mode,
    decode_satellite_init_response,
    decode_sensors,
    decode_zone_consigne,
    decode_zone_init,
    decode_zone_read,
    memory_address,
)
from frisquet_bridge.frame import ADDR_CONNECT, MSG_INIT, MSG_READ, Frame
from frisquet_bridge.model import BoilerData
from frisquet_bridge.transport.base import ReceivedFrame

log = logging.getLogger(__name__)

ZONE_IDS = {0x08: 1, 0x09: 2, 0x0A: 3}


def _consigne_addrs(zone: int, boiler_addr: int) -> set[int]:
    base = ADDR_SATELLITE_CONSIGNE + 0x0005 * (zone - 1)
    return {base, memory_address(base, boiler_addr=boiler_addr)}


class PassiveMirror:
    """Sniff RF traffic and mirror boiler responses into the data model."""

    def __init__(
        self,
        data: BoilerData,
        *,
        boiler_addr: int = 0x80,
        on_update: Callable[[], Awaitable[None]] | None = None,
        on_zone_config: Callable[[], None] | None = None,
    ) -> None:
        self._data = data
        self._boiler_addr = boiler_addr
        self._on_update = on_update
        self._on_zone_config = on_zone_config
        # READ request address keyed by (association_id, request_id) so the
        # boiler's response can be matched back to the address that was asked.
        self._pending_reads: dict[tuple[int, int], int] = {}

    def _persist_zone_config(self) -> None:
        if self._on_zone_config is not None:
            self._on_zone_config()

    async def handle(self, received: ReceivedFrame) -> None:
        frame = received.frame

        # Connect (or a real Connect box) querying the boiler: remember the
        # READ address so we can decode the boiler's matching response below.
        if frame.from_addr == ADDR_CONNECT and frame.to_addr == self._boiler_addr:
            if frame.msg_type == MSG_INIT:
                self._handle_connect_init_request(frame)
            elif frame.msg_type == MSG_READ and len(frame.payload) >= 2:
                addr = int.from_bytes(frame.payload[0:2], "big")
                self._pending_reads[(frame.association_id, frame.request_id)] = addr
            return

        # Satellites report state to the boiler at 0xa154 (zone config: schedule
        # + setpoints) and 0xa02f (live ambient/setpoint/mode). The boiler never
        # serves these to Connect, so sniffing the satellite is the only way to
        # learn them.
        if frame.to_addr == self._boiler_addr and frame.msg_type == MSG_INIT:
            zone = ZONE_IDS.get(frame.from_addr)
            if zone is not None and self._handle_satellite_report(zone, frame):
                return

        if frame.from_addr != self._boiler_addr or frame.to_addr != ADDR_CONNECT:
            return

        if frame.msg_type == MSG_READ:
            addr = self._pending_reads.pop((frame.association_id, frame.request_id), None)
            self._handle_read_response(addr, frame.payload)
        elif frame.msg_type == MSG_INIT:
            self._handle_boiler_init_response(frame)

    def _handle_satellite_report(self, zone: int, frame: Frame) -> bool:
        if frame.payload[:2] == b"\xa1\x54":
            with contextlib.suppress(ValueError):
                decode_zone_read(frame.payload, zone, self._data)
                self._persist_zone_config()
            return True
        write_addr = int.from_bytes(frame.payload[4:6], "big") if len(frame.payload) >= 6 else 0
        if write_addr in _consigne_addrs(zone, self._boiler_addr):
            with contextlib.suppress(ValueError):
                decode_zone_consigne(frame.payload, zone, self._data)
            return True
        return False

    def _handle_connect_init_request(self, frame: Frame) -> None:
        if len(frame.payload) < 6:
            return
        zone = ZONE_IDS.get(frame.control & 0x7F)
        write_addr = int.from_bytes(frame.payload[4:6], "big")
        if zone and write_addr == ADDR_ZONE_CONFIG:
            with contextlib.suppress(ValueError):
                decode_zone_init(frame.payload, zone, self._data)
                self._persist_zone_config()

    def _handle_boiler_init_response(self, frame: Frame) -> None:
        # The control byte's low nibble identifies the peer: a zone-satellite id
        # (0x08/0x09/0x0a) marks a relayed zone-config ACK whose body carries the
        # schedule/setpoints; anything else (e.g. 0x01) is a satellite_info block.
        zone = ZONE_IDS.get(frame.control & 0x7F)
        if zone is not None:
            with contextlib.suppress(ValueError):
                decode_zone_read(frame.payload, zone, self._data)
                self._persist_zone_config()
            return
        if len(frame.payload) >= 43:
            with contextlib.suppress(ValueError):
                decode_satellite_init_response(frame.payload, self._data)

    def _handle_read_response(self, addr: int | None, payload: bytes) -> None:
        if addr is None:
            return
        sensors = (ADDR_SENSORS, memory_address(ADDR_SENSORS, boiler_addr=self._boiler_addr))
        if addr in sensors:
            try:
                decode_sensors(payload, self._data)
            except ValueError:
                log.debug("ignored short sensors payload")
            return
        consumption_live = (
            ADDR_CONSUMPTION,
            memory_address(ADDR_CONSUMPTION, boiler_addr=self._boiler_addr),
        )
        daily_consumption = (
            ADDR_DAILY_CONSUMPTION,
            memory_address(ADDR_DAILY_CONSUMPTION, boiler_addr=self._boiler_addr),
        )
        if addr in consumption_live:
            with contextlib.suppress(ValueError):
                decode_consumption(payload, self._data)
            return
        if addr in daily_consumption:
            with contextlib.suppress(ValueError):
                decode_daily_consumption(payload, self._data)
            return
        dhw_mode_addrs = (ADDR_DHW_MODE, memory_address(ADDR_DHW_MODE, boiler_addr=self._boiler_addr))
        if addr in dhw_mode_addrs:
            with contextlib.suppress(ValueError):
                decode_dhw_mode(payload, self._data)
            return
        clock_addrs = (ADDR_DATE, memory_address(ADDR_DATE, boiler_addr=self._boiler_addr))
        if addr in clock_addrs:
            with contextlib.suppress(ValueError):
                decode_clock(payload, self._data)

    async def notify(self) -> None:
        if self._on_update is not None:
            await self._on_update()
