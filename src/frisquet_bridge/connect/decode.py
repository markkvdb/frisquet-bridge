"""Decode boiler memory blocks into the data model."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import UTC, datetime

from frisquet_bridge.connect.codec import BoilerDate, decode_pressure16, decode_temp16
from frisquet_bridge.model import (
    OUTSIDE_TEMP_ABSENT,
    BoilerData,
    BoilerStatus,
    DhwMode,
    ZoneMode,
    ZoneSchedule,
    ZoneState,
)

ADDR_SENSORS = 0x79E0
ADDR_SENSORS_APP = 0x79FC
# Live Connect traffic reads total consumption at 0x7a34. The app also reads
# 0x7a18 for daily/period consumption counters.
ADDR_CONSUMPTION = 0x7A34
ADDR_DAILY_CONSUMPTION = 0x7A18
ADDR_DHW_MODE = 0xA0FC
ADDR_DATE = 0xA02B
ADDR_SATELLITE_INFO = 0xA029
# Per-zone satellite setpoint block: zone 1 at 0xa02f, +5 per zone.
ADDR_SATELLITE_CONSIGNE = 0xA02F
# 0xa0f0 is the holiday / derogation ("vacances") block. The boiler also pushes
# it (with an empty date body) whenever a related program/manual/DHW change is
# committed, so a zero body means "no holiday dates", not "holiday off command".
ADDR_ZONE_CONFIG = 0xA154
ADDR_HOLIDAY = 0xA0F0
BOILER_ALT_OFFSET = 0xC8
# Body marker / inner length byte seen at the start of the 0xa0f0 push body.
HOLIDAY_BODY_MARKER = 0x1A
# Tag embedded in the 0x45 BOILER_EVENT (also present in the 0x7a18 daily block).
BOILER_EVENT_MEMORY_TAG = bytes((0x0A, 0x80, 0x31))
BOILER_EVENT_KIND_SET = 0x01
BOILER_EVENT_KIND_CLEAR = 0x02
TEMP16_ABSENT = 127.0
TEMP16_EXTREME_ABSENT = 258.1


def memory_address(base: int, *, boiler_addr: int) -> int:
    return base + (BOILER_ALT_OFFSET if boiler_addr == 0x84 else 0)


def memory_addrs(base: int, boiler_addr: int) -> set[int]:
    return {base, memory_address(base, boiler_addr=boiler_addr)}


def _i16(data: bytes, offset: int) -> float:
    return decode_temp16(data[offset : offset + 2])


def _temp16(data: bytes, offset: int) -> float | None:
    value = _i16(data, offset)
    return None if value == TEMP16_ABSENT else value


def _plausible(value: float | None, low: float, high: float) -> float | None:
    if value is None:
        return None
    return value if low <= value <= high else None


def _outside_temp16(data: bytes, offset: int) -> float | None:
    value = _i16(data, offset)
    if value in (OUTSIDE_TEMP_ABSENT, TEMP16_EXTREME_ABSENT):
        return None
    return _plausible(value, -40.0, 80.0)


def decode_sensors(payload: bytes, data: BoilerData) -> None:
    """Decode the 0x79e0 sensor block (OpenFrisquetVisio / frisquet-connect layout)."""
    if len(payload) < 57:
        raise ValueError(f"sensors payload too short: {len(payload)} bytes")

    b = data.boiler
    b.dhw_temperature = _plausible(_temp16(payload, 1), 0.0, 95.0)
    b.cdc_temperature = _plausible(_temp16(payload, 3), 0.0, 95.0)
    data.zones[1].flow_temperature = _plausible(_temp16(payload, 5), 0.0, 95.0)
    data.zones[2].flow_temperature = _plausible(_temp16(payload, 7), 0.0, 95.0)
    data.zones[3].flow_temperature = _plausible(_temp16(payload, 9), 0.0, 95.0)
    b.cdc_safety_temperature = _plausible(_temp16(payload, 11), 0.0, 95.0)
    flue = _temp16(payload, 13)
    b.flue_temperature = _plausible(None if flue is None else flue / 2.0, 0.0, 300.0)
    b.dhw_power = _plausible(_i16(payload, 15), 0.0, 100.0)
    b.heating_power = _plausible(_i16(payload, 17), 0.0, 100.0)
    b.pressure = _plausible(decode_pressure16(payload[21:23]), 0.0, 5.0)
    b.dhw_instant_temperature = _plausible(_temp16(payload, 25), 0.0, 95.0)

    data.zones[1].ambient_temperature = _plausible(_temp16(payload, 37), 0.0, 45.0)
    data.zones[2].ambient_temperature = _plausible(_temp16(payload, 39), 0.0, 45.0)
    data.zones[3].ambient_temperature = _plausible(_temp16(payload, 41), 0.0, 45.0)
    data.zones[1].flow_setpoint_temperature = _plausible(_temp16(payload, 43), 0.0, 95.0)
    data.zones[2].flow_setpoint_temperature = _plausible(_temp16(payload, 45), 0.0, 95.0)
    data.zones[3].flow_setpoint_temperature = _plausible(_temp16(payload, 47), 0.0, 95.0)
    data.zones[1].setpoint_temperature = _plausible(_temp16(payload, 49), 0.0, 35.0)
    data.zones[2].setpoint_temperature = _plausible(_temp16(payload, 51), 0.0, 35.0)
    data.zones[3].setpoint_temperature = _plausible(_temp16(payload, 53), 0.0, 35.0)

    ext = _outside_temp16(payload, 55)
    if ext is not None:
        b.outside_temperature = ext


def decode_consumption(payload: bytes, data: BoilerData) -> None:
    if len(payload) < 5:
        raise ValueError(f"consumption payload too short: {len(payload)} bytes")
    data.boiler.dhw_consumption = struct.unpack(">h", payload[1:3])[0]
    data.boiler.heating_consumption = struct.unpack(">h", payload[3:5])[0]


def decode_daily_consumption(payload: bytes, data: BoilerData) -> None:
    if len(payload) < 23:
        raise ValueError(f"daily consumption payload too short: {len(payload)} bytes")
    data.boiler.daily_dhw_consumption = struct.unpack(">h", payload[19:21])[0]
    data.boiler.daily_heating_consumption = struct.unpack(">h", payload[21:23])[0]


def decode_dhw_mode(payload: bytes, data: BoilerData, *, frame_bits: int | None = None) -> None:
    if not payload:
        raise ValueError(f"dhw mode payload too short: {len(payload)} bytes")
    raw = payload[2] if len(payload) >= 3 else payload[0]
    bits = raw & 0x81 if frame_bits is None else frame_bits
    data.boiler.dhw_frame_bits = bits
    mode = DhwMode.from_byte(raw)
    if mode is not None:
        data.boiler.dhw_mode = mode
    data.boiler.dhw_frame_bits = bits


def decode_clock(payload: bytes, data: BoilerData) -> None:
    if len(payload) < 9:
        raise ValueError(f"clock payload too short: {len(payload)} bytes")
    date = BoilerDate.decode(payload[1:9])
    data.last_seen_date = date.isoformat()


def decode_boiler_status(byte: int, data: BoilerData) -> None:
    data.boiler.status = BoilerStatus.from_byte(byte)
    data.boiler.fault = bool(byte & 0b0000_0001)


def init_response_body(payload: bytes) -> bytes:
    if payload and payload[0] in (0x2A, 0x30):
        length = payload[0]
        end = 1 + length
        if len(payload) >= end:
            return payload[1:end]
    return payload


def decode_satellite_init_response(payload: bytes, data: BoilerData) -> None:
    if len(payload) >= 43:
        decode_satellite_info(payload, data)
        return
    body = init_response_body(payload)
    decode_satellite_info(body, data)


def decode_zone_read(payload: bytes, zone: int, data: BoilerData) -> None:
    """Decode zone config from a READ response or raw memory block."""
    for start in (0, 1, 2):
        if len(payload) - start < 48:
            continue
        block = payload[start : start + 48]
        if ZoneMode.from_byte(block[3]) is None:
            continue
        decode_zone_init(block, zone, data)
        return
    decode_zone_init(payload, zone, data)


def decode_zone_init(payload: bytes, zone: int, data: BoilerData) -> None:
    """Decode a zone INIT (0xa154) into zone state."""
    body = _zone_init_body(payload)
    if len(body) < 6:
        return
    from frisquet_bridge.connect.codec import decode_temp8

    zs = data.zones[zone]
    mode_options = body[4]
    boost = bool(mode_options & 0b0100_0000)
    if not boost:
        zs.comfort_temperature = decode_temp8(body[0])
    zs.reduced_temperature = decode_temp8(body[1])
    zs.frost_temperature = decode_temp8(body[2])
    mode = ZoneMode.from_byte(body[3])
    if mode is not None:
        zs.mode = mode
    zs.mode_options = mode_options
    if zs.mode == ZoneMode.AUTO:
        # Following the program sets the 0x10 bit; a fixed/derogation level uses
        # the 0x20/0x04 bits, so an override is "not following the schedule".
        zs.override = not bool(mode_options & 0x10)
        zs.auto_comfort = bool(mode_options & 0x01)
    else:
        zs.override = False
        zs.auto_comfort = None
    zs.boost = boost
    if len(body) >= 48:
        zs.schedule = ZoneSchedule.decode(body[6:48])


def decode_satellite_info(payload: bytes, data: BoilerData) -> None:
    """Decode the 0xa029 satellite/boiler state block returned to satellites."""
    if len(payload) < 43:
        raise ValueError(f"satellite info payload too short: {len(payload)} bytes")

    ext = _outside_temp16(payload, 1)
    if ext is not None:
        data.boiler.outside_temperature = ext

    date = BoilerDate.decode(payload[5:13])
    data.last_seen_date = date.isoformat()
    decode_boiler_status(payload[11], data)

    _decode_satellite_zone(payload, data, zone=1, offset=13)
    _decode_satellite_zone(payload, data, zone=2, offset=23)
    _decode_satellite_zone(payload, data, zone=3, offset=33)


def decode_zone_consigne(payload: bytes, zone: int, data: BoilerData) -> None:
    """Decode a satellite's setpoint INIT (0xa02f write) into zone state.

    Mirrors OpenFrisquetVisio ``donneesSatellite_t``: a 9-byte INIT header
    (read/write address + size + data length) followed by ambient(2),
    setpoint(2), unknown(1), mode(1), options(2).
    """
    body = payload[9:] if len(payload) >= 9 else b""
    if len(body) < 6:
        return
    zs = data.zones[zone]
    ambient = _temp16(body, 0)
    setpoint = _temp16(body, 2)
    if ambient is not None:
        zs.ambient_temperature = ambient
    if setpoint is not None:
        zs.setpoint_temperature = setpoint
    _set_satellite_mode(body[5], zs)


# (auto_comfort, derogation/override) -> satellite mode byte for an auto zone.
_AUTO_SATELLITE_MODE = {
    (True, True): 0x03,
    (True, False): 0x05,
    (False, True): 0x02,
    (False, False): 0x04,
}


def encode_satellite_mode(zone: ZoneState, *, connect_compat: bool = False) -> int:
    """Encode zone mode for a satellite setpoint frame (see Satellite::getMode).

    Optionally XORs 0x20 to match the Connect-compatible encoding the boiler
    expects when a Connect module participates in the network.
    """
    mode = zone.mode
    if mode == ZoneMode.FROST:
        raw = 0x10
    elif mode == ZoneMode.COMFORT:
        raw = 0x01
    elif mode == ZoneMode.REDUCED:
        raw = 0x00
    elif mode == ZoneMode.AUTO:
        raw = _AUTO_SATELLITE_MODE[(bool(zone.auto_comfort), bool(zone.override))]
    else:
        raise ValueError(f"zone {zone.zone}: mode unknown, cannot encode satellite consigne")
    return raw ^ 0x20 if connect_compat else raw


def _zone_init_body(payload: bytes) -> bytes:
    if len(payload) >= 15 and payload[4:6] == b"\xa1\x54":
        length = payload[8]
        return payload[9 : 9 + length]
    if len(payload) >= 7 and payload[0] in (0x2A, 0x30):
        length = payload[0]
        return payload[1 : 1 + length]
    return payload


def _decode_satellite_zone(payload: bytes, data: BoilerData, *, zone: int, offset: int) -> None:
    zs = data.zones[zone]
    ambient = _temp16(payload, offset)
    setpoint = _temp16(payload, offset + 2)
    if ambient is None and setpoint in (None, 0.0):
        return
    zs.ambient_temperature = ambient
    zs.setpoint_temperature = setpoint
    _set_satellite_mode(payload[offset + 5], zs)


def _set_satellite_mode(raw: int, zone: object) -> None:
    decoded = _decode_satellite_mode(raw)
    if decoded is None:
        return
    mode, auto_comfort, override = decoded
    zone.mode = mode
    zone.auto_comfort = auto_comfort
    zone.override = override


def _decode_satellite_mode(raw: int) -> tuple[ZoneMode, bool | None, bool] | None:
    for value in (raw, raw ^ 0x20):
        if value == 0x00:
            return ZoneMode.REDUCED, None, False
        if value == 0x01:
            return ZoneMode.COMFORT, None, False
        if value == 0x02:
            return ZoneMode.AUTO, False, True
        if value == 0x03:
            return ZoneMode.AUTO, True, True
        if value == 0x04:
            return ZoneMode.AUTO, False, False
        if value == 0x05:
            return ZoneMode.AUTO, True, False
        if value == 0x10:
            return ZoneMode.FROST, None, False
    return None


@dataclass(frozen=True, slots=True)
class MemoryPush:
    """Boiler-initiated memory block update (msg_type 0x10).

    ``trailer`` is the region after the length-delimited ``body``. For the
    0xa0f0 block the last trailer byte carries a change-toggle flag (the 0x80
    bit flips on each committed change; the low nibble has been 0x08).
    """

    address: int
    length: int
    body: bytes
    trailer: bytes = b""

    @property
    def toggle_flag(self) -> int | None:
        return self.trailer[-1] if self.trailer else None


@dataclass(frozen=True, slots=True)
class HolidayPush:
    """Decoded 0xa0f0 holiday/derogation body."""

    active: bool
    start: datetime | None
    end: datetime | None


@dataclass(frozen=True, slots=True)
class BoilerEventPush:
    """Boiler-initiated configuration event (msg_type 0x45).

    Observed to fire on a **maximum-temperature** (boiler parameter) change as a
    ``clear`` (kind 0x02) then ``set`` (kind 0x01) pair. ``stamp`` is a 4-byte
    field that increments between the pair but is not a plain timestamp; the
    changed value has not yet been localized, so ``tail`` is surfaced verbatim.
    """

    kind: int
    memory_tag: bytes
    stamp: bytes
    tail: bytes

    @property
    def kind_label(self) -> str:
        if self.kind == BOILER_EVENT_KIND_SET:
            return "set"
        if self.kind == BOILER_EVENT_KIND_CLEAR:
            return "clear"
        return f"0x{self.kind:02x}"


def memory_push_address(payload: bytes) -> int | None:
    if len(payload) < 2:
        return None
    return int.from_bytes(payload[0:2], "big")


def parse_memory_push(payload: bytes) -> MemoryPush:
    if len(payload) < 4:
        raise ValueError(f"memory push payload too short: {len(payload)} bytes")
    address = int.from_bytes(payload[0:2], "big")
    length = int.from_bytes(payload[2:4], "big")
    body = payload[4 : 4 + length]
    if len(body) != length:
        raise ValueError(f"memory push truncated: expected {length} body bytes, got {len(body)}")
    trailer = payload[4 + length :]
    return MemoryPush(address=address, length=length, body=body, trailer=trailer)


def parse_boiler_event_push(payload: bytes) -> BoilerEventPush:
    if len(payload) != 21:
        raise ValueError(f"boiler event payload must be 21 bytes, got {len(payload)}")
    return BoilerEventPush(
        kind=payload[0],
        memory_tag=payload[1:4],
        stamp=payload[4:8],
        tail=payload[8:21],
    )


def decode_frisquet_timestamp(data: bytes) -> datetime | None:
    """Decode the 4-byte Frisquet timestamp (holiday.rs byte order 2,3,0,1)."""
    if len(data) < 4:
        return None
    secs = (data[2] << 24) + (data[3] << 16) + (data[0] << 8) + data[1]
    try:
        return datetime.fromtimestamp(secs, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def decode_holiday_push(body: bytes, data: BoilerData) -> HolidayPush:
    """Decode the 0xa0f0 holiday body: ``marker(1) start(4) mid(4) end(4)``.

    Holiday is considered active when the start date field is non-zero. A fully
    zeroed date body means no holiday is scheduled (the boiler also emits this
    form on unrelated program/manual/DHW commits, so it is not a holiday-off
    command in itself).
    """
    if len(body) < 13:
        raise ValueError(f"holiday push body too short: {len(body)} bytes")
    start_raw = body[1:5]
    end_raw = body[9:13]
    active = start_raw != b"\x00\x00\x00\x00"
    start = decode_frisquet_timestamp(start_raw) if active else None
    end = decode_frisquet_timestamp(end_raw) if active else None
    data.boiler.holiday_active = active
    return HolidayPush(active=active, start=start, end=end)


def decode_memory_push(payload: bytes, data: BoilerData, *, boiler_addr: int = 0x80) -> MemoryPush:
    """Parse a MEMORY_PUSH (0x10) and update ``data`` when the block is known."""
    push = parse_memory_push(payload)
    if push.address in memory_addrs(ADDR_HOLIDAY, boiler_addr):
        decode_holiday_push(push.body, data)
    elif push.address in memory_addrs(ADDR_DHW_MODE, boiler_addr):
        decode_dhw_mode(push.body, data)
    return push


def decode_boiler_event(payload: bytes, data: BoilerData) -> BoilerEventPush:
    """Parse a BOILER_EVENT (0x45). Currently informational only (see class)."""
    return parse_boiler_event_push(payload)


def memory_push_block_name(address: int, *, boiler_addr: int = 0x80) -> str | None:
    if address in memory_addrs(ADDR_HOLIDAY, boiler_addr):
        return "holiday"
    if address in memory_addrs(ADDR_DHW_MODE, boiler_addr):
        return "dhw_mode"
    return None


def describe_memory_push(payload: bytes, *, boiler_addr: int = 0x80) -> str:
    data = BoilerData()
    try:
        push = parse_memory_push(payload)
    except ValueError as exc:
        return f"MEMORY_PUSH decode error: {exc}"

    name = memory_push_block_name(push.address, boiler_addr=boiler_addr) or f"0x{push.address:04x}"
    parts = [f"MEMORY_PUSH addr={name} len=0x{push.length:04x}"]

    if push.address in memory_addrs(ADDR_HOLIDAY, boiler_addr):
        try:
            holiday = decode_holiday_push(push.body, data)
        except ValueError:
            holiday = None
        if holiday is None:
            parts.append(f"body={push.body.hex()}")
        elif holiday.active:
            parts.append("holiday=on")
            if holiday.start is not None:
                parts.append(f"start={holiday.start.isoformat(timespec='seconds')}")
            if holiday.end is not None:
                parts.append(f"end={holiday.end.isoformat(timespec='seconds')}")
        else:
            parts.append("holiday=none")
        if push.toggle_flag is not None:
            parts.append(f"flag=0x{push.toggle_flag:02x}")
    elif push.address in memory_addrs(ADDR_DHW_MODE, boiler_addr):
        decode_dhw_mode(push.body, data)
        mode = data.boiler.dhw_mode.value if data.boiler.dhw_mode is not None else "unknown"
        parts.append(f"dhw_mode={mode}")
    elif push.body:
        parts.append(f"body={push.body.hex()}")

    return " ".join(parts)


def describe_boiler_event(payload: bytes) -> str:
    try:
        event = parse_boiler_event_push(payload)
    except ValueError as exc:
        return f"BOILER_EVENT decode error: {exc}"

    tag = event.memory_tag.hex()
    parts = [
        "BOILER_EVENT",
        f"kind={event.kind_label}",
        f"tag=0x{tag}",
        f"stamp={event.stamp.hex()}",
    ]
    if event.tail != b"\x00" * 13:
        parts.append(f"tail={event.tail.hex()}")
    return " ".join(parts)
