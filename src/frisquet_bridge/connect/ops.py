"""High-level read/write operations against the boiler."""

from __future__ import annotations

import contextlib
import struct

import structlog

from frisquet_bridge.climate import resolve_zone_intent
from frisquet_bridge.connect.client import FrisquetClient
from frisquet_bridge.connect.codec import encode_temp8, encode_temp16
from frisquet_bridge.connect.decode import (
    ADDR_CONSUMPTION,
    ADDR_DAILY_CONSUMPTION,
    ADDR_DATE,
    ADDR_DHW_MODE,
    ADDR_SATELLITE_CONSIGNE,
    ADDR_SATELLITE_INFO,
    ADDR_SENSORS,
    ADDR_ZONE_CONFIG,
    decode_clock,
    decode_consumption,
    decode_daily_consumption,
    decode_dhw_mode,
    decode_satellite_init_response,
    decode_sensors,
    encode_satellite_mode,
    memory_address,
)
from frisquet_bridge.frame import ADDR_SATELLITE_Z1, MSG_INIT, MSG_SONDE_INIT
from frisquet_bridge.model import SCHEDULE_DAYS, BoilerData, DhwMode, ZoneMode, ZoneState

ZONE_ADDR = ADDR_ZONE_CONFIG
SONDE_TEMP_PREFIX = bytes((0x9C, 0x54, 0x00, 0x04, 0xA0, 0x29, 0x00, 0x01, 0x02))
ZONE_MODE_OPTIONS_DEFAULT = 0b0010_0100
# Options byte the satellite expects, keyed by mode class (matching the official
# app). The high bits encode schedule vs fixed-level vs frost; a derogation is
# expressed by keeping the mode byte AUTO while using the fixed-level options,
# so there is no separate derogation bit.
ZONE_OPTS_SCHEDULE = 0x10  # AUTO, following the weekly program
ZONE_OPTS_FROST = 0x20  # Hors-gel / off
ZONE_OPTS_FIXED = 0x24  # fixed comfort/eco level (permanent or auto derogation)
ZONE_OPTS_COMFORT_BIT = 0x01  # comfort (vs eco) within a fixed level
ZONE_OPTS_BOOST_BIT = 0x40


def _zone_mode_options(mode: ZoneMode, *, auto_comfort: bool, override: bool, boost: bool) -> int:
    if mode == ZoneMode.FROST:
        return ZONE_OPTS_FROST
    if mode == ZoneMode.AUTO and not override:
        return ZONE_OPTS_SCHEDULE
    opts = ZONE_OPTS_FIXED
    if auto_comfort:
        opts |= ZONE_OPTS_COMFORT_BIT
    if boost:
        opts |= ZONE_OPTS_BOOST_BIT
    return opts
# Relayed zone writes get no boiler ACK and the satellite only catches a
# relayed frame inside a short RX window, so (like the official app) we re-send
# the write several times spaced ~1.5s apart rather than in one tight burst.
ZONE_WRITE_REPEATS = 6
ZONE_WRITE_INTERVAL = 1.5
log = structlog.get_logger(__name__)


def _zone_id(zone: int) -> int:
    return {1: ADDR_SATELLITE_Z1, 2: 0x09, 3: 0x0A}[zone]


def _zone_metadata_complete(zone: ZoneState) -> bool:
    # A zone write only needs the three setpoints + mode options. The weekly
    # schedule is optional: when known we send the full 0x18-byte block (like
    # the official app), otherwise we fall back to OpenFrisquetVisio's short
    # 0x03 write which omits the schedule.
    return (
        zone.comfort_temperature is not None
        and zone.reduced_temperature is not None
        and zone.frost_temperature is not None
        and zone.mode_options is not None
    )


class BoilerOps:
    """Protocol operations that populate the shared data model."""

    def __init__(self, client: FrisquetClient, *, boiler_addr: int, memory_offset: int = 0) -> None:
        self._client = client
        self._boiler_addr = boiler_addr
        self._offset = memory_offset

    def _addr(self, base: int) -> int:
        return memory_address(base, boiler_addr=self._boiler_addr)

    def _satellite_info_init_payload(self) -> bytes:
        return struct.pack(
            ">HHHH",
            self._addr(ADDR_SATELLITE_INFO),
            0x0015,
            self._addr(0xA02F),
            0x0004,
        ) + bytes((0x08, 0x01, 0x0D, 0x00, 0x50, 0x00, 0x14, 0x00, 0x00, 0x00))

    async def read_sensors(self, data: BoilerData) -> None:
        payload = await self._client.read_memory(self._addr(ADDR_SENSORS), 0x001C)
        decode_sensors(payload, data)

    async def read_consumption(self, data: BoilerData) -> None:
        payload = await self._client.read_memory(self._addr(ADDR_CONSUMPTION), 0x001C)
        decode_consumption(payload, data)

    async def read_daily_consumption(self, data: BoilerData) -> None:
        payload = await self._client.read_memory(self._addr(ADDR_DAILY_CONSUMPTION), 0x001C)
        decode_daily_consumption(payload, data)

    async def read_dhw_mode(self, data: BoilerData) -> None:
        payload = await self._client.read_memory(self._addr(ADDR_DHW_MODE), 0x0001)
        decode_dhw_mode(payload, data)

    async def read_clock(self, data: BoilerData) -> None:
        payload = await self._client.read_memory(self._addr(ADDR_DATE), 0x0004)
        decode_clock(payload, data)

    async def read_satellite_info(self, data: BoilerData) -> None:
        response = await self._client.request(
            control=0x01,
            msg_type=MSG_INIT,
            payload=self._satellite_info_init_payload(),
        )
        decode_satellite_init_response(response.payload, data)
        log.debug("satellite_info_read")

    async def ensure_zone_metadata(self, zone: int, data: BoilerData) -> None:
        # The boiler does not serve zone config to Connect; schedule/setpoints
        # are learned passively from the satellite's 0xa154 broadcasts. A write
        # therefore requires that we have already heard from the satellite.
        if not _zone_metadata_complete(data.zones[zone]):
            raise ValueError(
                f"zone {zone}: setpoints/mode not yet learned "
                "(waiting for satellite 0xa154 broadcast or a zone write)"
            )

    async def write_dhw_mode(self, data: BoilerData, mode: DhwMode) -> None:
        raw = (mode.byte & 0x7E) | data.boiler.dhw_frame_bits
        body = struct.pack(">HHHHB", self._addr(ADDR_DHW_MODE), 1, self._addr(ADDR_DHW_MODE), 1, 2)
        body += bytes((0x00, raw))
        await self._client.request(control=0x01, msg_type=MSG_INIT, payload=body)
        data.boiler.dhw_mode = mode
        log.info("dhw_mode_written", mode=mode.value, raw=f"0x{raw:02x}")

    async def write_zone_short(
        self,
        zone: int,
        data: BoilerData,
        *,
        mode: ZoneMode | None = None,
        boost: bool | None = None,
        override: bool | None = None,
        auto_comfort: bool | None = None,
        comfort_temperature: float | None = None,
        reduced_temperature: float | None = None,
        frost_temperature: float | None = None,
    ) -> None:
        """Send a short zone INIT (mode/setpoints) like OpenFrisquetVisio envoyerZone."""
        await self.ensure_zone_metadata(zone, data)
        zs = data.zones[zone]
        if mode is None and zs.mode is not None:
            mode = zs.mode
        if mode is None:
            raise ValueError(f"zone {zone}: mode unknown")

        boost = zs.boost if boost is None else boost
        override = zs.override if override is None else override
        auto_comfort = zs.auto_comfort if auto_comfort is None else auto_comfort
        if mode != ZoneMode.AUTO:
            auto_comfort = mode == ZoneMode.COMFORT
            override = False
        if boost and mode != ZoneMode.COMFORT:
            raise ValueError("zone boost can only be enabled in Comfort mode")

        comfort_val = comfort_temperature if comfort_temperature is not None else zs.comfort_temperature
        reduced_val = reduced_temperature if reduced_temperature is not None else zs.reduced_temperature
        frost_val = frost_temperature if frost_temperature is not None else zs.frost_temperature
        if comfort_val is None or reduced_val is None or frost_val is None:
            raise ValueError(f"zone {zone}: comfort/reduced/frost setpoints unknown")

        opts = _zone_mode_options(mode, auto_comfort=bool(auto_comfort), override=bool(override), boost=bool(boost))

        comfort = encode_temp8(comfort_val + (2.0 if boost else 0.0))
        reduced = encode_temp8(reduced_val)
        frost = encode_temp8(frost_val)
        body = bytes((comfort, reduced, frost, mode.byte, opts, 0x00))
        schedule = _zone_schedule_bytes(data, zone)
        if schedule is not None:
            # Full write (official-app style): setpoints + mode + weekly schedule.
            payload = struct.pack(">HHHHB", self._addr(ZONE_ADDR), 0x0018, self._addr(ZONE_ADDR), 0x0018, 0x30)
            payload += body + schedule
        else:
            # Short write (OpenFrisquetVisio style): setpoints + mode only.
            payload = struct.pack(">HHHHB", self._addr(ZONE_ADDR), 0x0018, self._addr(ZONE_ADDR), 0x0003, 0x06)
            payload += body

        # A zone write targets the satellite (control = zone id): the boiler
        # silently relays it and never ACKs Connect directly, so we fire-and-
        # forget and re-send a short burst for reliability, like the official
        # app. The satellite's 0xa154 rebroadcast (decoded by PassiveMirror)
        # is what confirms the change.
        await self._client.send_oneway(
            control=_zone_id(zone),
            msg_type=MSG_INIT,
            payload=payload,
            repeats=ZONE_WRITE_REPEATS,
            interval=ZONE_WRITE_INTERVAL,
        )
        zs.mode = mode
        zs.mode_options = opts
        zs.auto_comfort = auto_comfort if mode == ZoneMode.AUTO else None
        zs.override = bool(override) if mode == ZoneMode.AUTO else False
        zs.boost = boost
        zs.comfort_temperature = comfort_val
        zs.reduced_temperature = reduced_val
        zs.frost_temperature = frost_val
        log.info(
            "zone_written",
            zone=zone,
            mode=mode.value,
            boost=boost,
            override=zs.override,
            auto_comfort=zs.auto_comfort,
            mode_options=f"0x{opts:02x}",
            schedule_known=zs.schedule is not None,
        )

    async def apply_zone_climate(
        self,
        zone: int,
        data: BoilerData,
        *,
        hvac_mode: str | None = None,
        preset: str | None = None,
        target_temperature: float | None = None,
    ) -> None:
        await self.ensure_zone_metadata(zone, data)
        intent = resolve_zone_intent(
            data.zones[zone],
            hvac_mode=hvac_mode,
            preset=preset,
            target_temperature=target_temperature,
        )
        await self.write_zone_short(
            zone,
            data,
            mode=intent.mode,
            boost=intent.boost,
            override=intent.override,
            auto_comfort=intent.auto_comfort,
            comfort_temperature=intent.comfort_temperature,
            reduced_temperature=intent.reduced_temperature,
            frost_temperature=intent.frost_temperature,
        )

    async def write_zone_mode(self, zone: int, data: BoilerData, mode: ZoneMode, *, boost: bool = False) -> None:
        await self.write_zone_short(zone, data, mode=mode, boost=boost, override=False)

    async def write_zone_boost(self, zone: int, data: BoilerData, boost: bool) -> None:
        zs = data.zones[zone]
        mode = zs.mode
        if mode is None:
            raise ValueError(f"zone {zone}: mode unknown")
        await self.write_zone_short(zone, data, mode=mode, boost=boost)

    async def write_zone_override(self, zone: int, data: BoilerData, value: str) -> None:
        normalized = value.strip().casefold().replace("é", "e").replace("_", " ").replace("-", " ")
        if normalized in {"none", "off", "clear", "auto"}:
            await self.write_zone_short(zone, data, mode=ZoneMode.AUTO, boost=False, override=False)
            return
        if normalized in {"comfort", "confort"}:
            await self.write_zone_short(zone, data, mode=ZoneMode.AUTO, boost=False, override=True, auto_comfort=True)
            return
        if normalized in {"reduced", "reduit", "eco"}:
            await self.write_zone_short(zone, data, mode=ZoneMode.AUTO, boost=False, override=True, auto_comfort=False)
            return
        raise ValueError(f"zone {zone}: override must be one of none, comfort, reduced")

    async def write_outside_temperature(self, data: BoilerData, temperature: float) -> None:
        temp = max(-30.0, min(80.0, round(temperature * 10) / 10))
        body = SONDE_TEMP_PREFIX + encode_temp16(temp)
        await self._client.request(
            control=0x01,
            msg_type=MSG_INIT,
            payload=body,
            from_addr=0x20,
        )
        data.sonde.outside_temperature = temp
        log.info("outside_temperature_written", temperature=temp)

    async def send_zone_consigne(
        self,
        zone: int,
        data: BoilerData,
        *,
        ambient: float,
        setpoint: float,
        connect_compat: bool = False,
    ) -> None:
        """Send a zone setpoint as a (virtual) satellite would (0xa02f write).

        Mirrors OpenFrisquetVisio ``Satellite::envoyerConsigne``: writes
        ambient + active setpoint + encoded mode to the boiler and decodes the
        boiler-state block returned in the response.
        """
        zs = data.zones[zone]
        mode_byte = encode_satellite_mode(zs, connect_compat=connect_compat)
        write_addr = self._addr(ADDR_SATELLITE_CONSIGNE) + 0x0005 * (zone - 1)
        body = struct.pack(">HHHHB", self._addr(ADDR_SATELLITE_INFO), 0x0015, write_addr, 0x0004, 0x08)
        body += encode_temp16(ambient) + encode_temp16(setpoint) + bytes((0x00, mode_byte)) + struct.pack(">H", 0x0000)
        response = await self._client.request(control=0x01, msg_type=MSG_INIT, payload=body)
        with contextlib.suppress(ValueError):
            decode_satellite_init_response(response.payload, data)
        log.info(
            "zone_consigne_sent",
            zone=zone,
            ambient=ambient,
            setpoint=setpoint,
            mode=zs.mode.value if zs.mode is not None else None,
            mode_byte=f"0x{mode_byte:02x}",
        )

    async def sonde_init(self) -> None:
        await self._client.request(
            control=0x01,
            msg_type=MSG_SONDE_INIT,
            payload=b"\x00\x00",
            from_addr=0x20,
        )


def ops_from_config(client: FrisquetClient, *, boiler_addr: int, memory_offset: int) -> BoilerOps:
    return BoilerOps(client, boiler_addr=boiler_addr, memory_offset=memory_offset)


def _zone_schedule_bytes(data: BoilerData, zone: int) -> bytes | None:
    schedule = data.zones[zone].schedule
    if schedule is None:
        return None
    return b"".join(schedule.days[day] for day in SCHEDULE_DAYS)
