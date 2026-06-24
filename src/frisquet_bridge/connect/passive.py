"""Passive helpers for interpreting sniffed Connect read traffic."""

from __future__ import annotations

import logging

from frisquet_bridge.connect.decode import (
    ADDR_CONSUMPTION,
    ADDR_DAILY_CONSUMPTION,
    ADDR_DATE,
    ADDR_DHW_MODE,
    ADDR_HOLIDAY,
    ADDR_SENSORS,
    ADDR_SENSORS_APP,
    decode_clock,
    decode_consumption,
    decode_daily_consumption,
    decode_dhw_mode,
    decode_satellite_info,
    decode_sensors,
    decode_zone_init,
    describe_boiler_event,
    describe_memory_push,
    memory_address,
)
from frisquet_bridge.frame import ADDR_BOILER, MSG_BOILER_EVENT, MSG_INIT, MSG_MEMORY_PUSH, MSG_READ, Frame
from frisquet_bridge.model import BoilerData

log = logging.getLogger(__name__)
ZONE_IDS = {0x08: 1, 0x09: 2, 0x0A: 3}
ADDR_SATELLITE_INFO = 0xA029
ADDR_ZONE_CONFIG = 0xA154


class PassiveReadTracker:
    """Track READ request/response pairs and describe known responses."""

    def __init__(self, *, boiler_addr: int = ADDR_BOILER) -> None:
        self._boiler_addr = boiler_addr
        self._last_reads: dict[tuple[int, int, int], int] = {}
        self._last_init_reads: dict[tuple[int, int], int] = {}
        self._last_init_writes: dict[tuple[int, int], int] = {}

    def describe(self, frame: Frame) -> str | None:
        if frame.msg_type == MSG_MEMORY_PUSH:
            return self._describe_memory_push(frame)
        if frame.msg_type == MSG_BOILER_EVENT:
            return self._describe_boiler_event(frame)

        if frame.msg_type == MSG_INIT:
            return self._describe_init(frame)

        if frame.msg_type != MSG_READ:
            return None

        if frame.to_addr == self._boiler_addr and len(frame.payload) >= 4:
            addr = int.from_bytes(frame.payload[0:2], "big")
            size = int.from_bytes(frame.payload[2:4], "big")
            key = (frame.from_addr, frame.association_id, frame.request_id)
            self._last_reads[key] = addr
            name = read_addr_name(addr, self._boiler_addr)
            return f"READ request addr=0x{addr:04x} size=0x{size:04x}{_suffix(name)}"

        if frame.from_addr != self._boiler_addr:
            return None

        key = (frame.to_addr, frame.association_id, frame.request_id)
        addr = self._last_reads.get(key)
        if addr is None:
            return None

        if addr in sensor_addrs(self._boiler_addr):
            return _describe_sensors(frame.payload)
        if addr in memory_addrs(ADDR_DHW_MODE, self._boiler_addr):
            return _describe_dhw_mode(frame.payload)
        if addr in memory_addrs(ADDR_DATE, self._boiler_addr):
            return _describe_clock(frame.payload)
        if addr in daily_consumption_addrs(self._boiler_addr):
            return _describe_daily_consumption(frame.payload)
        if addr not in consumption_addrs(self._boiler_addr):
            name = read_addr_name(addr, self._boiler_addr)
            return f"READ response addr=0x{addr:04x} len={len(frame.payload)}{_suffix(name)}"

        data = BoilerData()
        try:
            decode_consumption(frame.payload, data)
        except ValueError as exc:
            log.warning("consumption response could not be decoded: %s", exc)
            return f"CONSUMPTION response too short len={len(frame.payload)}"

        return (
            "CONSUMPTION "
            f"dhw={data.boiler.dhw_consumption} kWh "
            f"heating={data.boiler.heating_consumption} kWh"
        )

    def _describe_init(self, frame: Frame) -> str | None:
        key = (frame.association_id, frame.request_id)
        init_read = _init_read_addr(frame.payload)
        init_write = _init_write_addr(frame.payload)
        if init_read is not None:
            self._last_init_reads[key] = init_read
        if init_write is not None:
            self._last_init_writes[key] = init_write

        if init_read == ADDR_SATELLITE_INFO:
            return "INIT request addr=0xa029 (satellite_info)"
        if init_write == ADDR_ZONE_CONFIG:
            return _describe_zone_init(frame)

        if self._last_init_writes.get(key) == ADDR_ZONE_CONFIG:
            return _describe_zone_init(frame, allow_length_prefixed=True)
        if self._last_init_reads.get(key) == ADDR_SATELLITE_INFO:
            return _describe_satellite_info(frame)
        return None

    def _describe_memory_push(self, frame: Frame) -> str | None:
        if frame.from_addr != self._boiler_addr or not frame.payload:
            return None
        return describe_memory_push(frame.payload, boiler_addr=self._boiler_addr)

    def _describe_boiler_event(self, frame: Frame) -> str | None:
        if frame.from_addr != self._boiler_addr or not frame.payload:
            return None
        return describe_boiler_event(frame.payload)


def consumption_addrs(boiler_addr: int) -> set[int]:
    return memory_addrs(ADDR_CONSUMPTION, boiler_addr)


def daily_consumption_addrs(boiler_addr: int) -> set[int]:
    return memory_addrs(ADDR_DAILY_CONSUMPTION, boiler_addr)


def read_addr_name(addr: int, boiler_addr: int) -> str | None:
    if addr in memory_addrs(ADDR_SENSORS, boiler_addr):
        return "sensors"
    if addr in memory_addrs(ADDR_SENSORS_APP, boiler_addr):
        return "sensors_app"
    if addr in memory_addrs(ADDR_CONSUMPTION, boiler_addr):
        return "consumption"
    if addr in memory_addrs(ADDR_DAILY_CONSUMPTION, boiler_addr):
        return "daily_consumption"
    if addr in memory_addrs(ADDR_DHW_MODE, boiler_addr):
        return "dhw_mode"
    if addr in memory_addrs(ADDR_HOLIDAY, boiler_addr):
        return "holiday"
    if addr in memory_addrs(ADDR_DATE, boiler_addr):
        return "clock"
    return None


def sensor_addrs(boiler_addr: int) -> set[int]:
    return memory_addrs(ADDR_SENSORS, boiler_addr) | memory_addrs(ADDR_SENSORS_APP, boiler_addr)


def memory_addrs(base: int, boiler_addr: int) -> set[int]:
    return {base, memory_address(base, boiler_addr=boiler_addr)}


def _suffix(name: str | None) -> str:
    return f" ({name})" if name else ""


def _describe_sensors(payload: bytes) -> str:
    data = BoilerData()
    try:
        decode_sensors(payload, data)
    except ValueError as exc:
        log.warning("sensors response could not be decoded: %s", exc)
        return f"SENSORS response too short len={len(payload)}"
    b = data.boiler
    return (
        "SENSORS "
        f"dhw={_fmt(b.dhw_temperature)}°C "
        f"cdc={_fmt(b.cdc_temperature)}°C "
        f"dhw_power={_fmt(b.dhw_power)}kW "
        f"heating_power={_fmt(b.heating_power)}kW "
        f"pressure={_fmt(b.pressure)}bar"
    )


def _describe_daily_consumption(payload: bytes) -> str:
    data = BoilerData()
    try:
        decode_daily_consumption(payload, data)
    except ValueError as exc:
        log.warning("daily consumption response could not be decoded: %s", exc)
        return f"DAILY_CONSUMPTION response too short len={len(payload)}"

    return (
        "DAILY_CONSUMPTION "
        f"dhw={data.boiler.daily_dhw_consumption} kWh "
        f"heating={data.boiler.daily_heating_consumption} kWh"
    )


def _describe_dhw_mode(payload: bytes) -> str:
    data = BoilerData()
    try:
        decode_dhw_mode(payload, data)
    except ValueError as exc:
        log.warning("dhw mode response could not be decoded: %s", exc)
        return f"DHW_MODE response too short len={len(payload)}"
    return f"DHW_MODE mode={data.boiler.dhw_mode or 'unknown'}"


def _describe_clock(payload: bytes) -> str:
    data = BoilerData()
    try:
        decode_clock(payload, data)
    except ValueError as exc:
        log.warning("clock response could not be decoded: %s", exc)
        return f"CLOCK response too short len={len(payload)}"
    return f"CLOCK {data.last_seen_date or 'unknown'}"


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def _describe_zone_init(frame: Frame, *, allow_length_prefixed: bool = False) -> str | None:
    zone = _frame_zone(frame)
    if zone is None or not _looks_like_zone_init(frame.payload, allow_length_prefixed=allow_length_prefixed):
        return None

    data = BoilerData()
    decode_zone_init(frame.payload, zone, data)
    z = data.zones[zone]
    mode = z.mode.value if z.mode is not None else "unknown"
    boost = "on" if z.boost else "off"
    detail = (
        f"ZONE{zone} mode={mode} boost={boost} "
        f"comfort={_fmt(z.comfort_temperature)}°C "
        f"reduced={_fmt(z.reduced_temperature)}°C "
        f"frost={_fmt(z.frost_temperature)}°C"
    )
    if z.auto_comfort is not None:
        detail += f" auto={'comfort' if z.auto_comfort else 'reduced'}"
    if z.override:
        detail += " override=on"
    if z.schedule is not None:
        detail += f" schedule_raw={z.schedule.compact_hex()}"
    return detail


def _frame_zone(frame: Frame) -> int | None:
    for addr in (frame.from_addr, frame.to_addr, frame.control & 0x7F):
        if addr in ZONE_IDS:
            return ZONE_IDS[addr]
    return None


def _looks_like_zone_init(payload: bytes, *, allow_length_prefixed: bool = False) -> bool:
    if len(payload) >= 15 and payload[4:6] == b"\xa1\x54":
        return True
    return allow_length_prefixed and len(payload) >= 7 and payload[0] in (0x2A, 0x30)


def _describe_satellite_info(frame: Frame) -> str | None:
    zone = _frame_zone(frame)
    if zone is None or not frame.is_ack:
        return None
    data = BoilerData()
    try:
        decode_satellite_info(frame.payload, data)
    except ValueError as exc:
        log.warning("satellite info response could not be decoded: %s", exc)
        return f"SATELLITE_INFO response too short len={len(frame.payload)}"
    b = data.boiler
    status = b.status.value if b.status is not None else "unknown"
    parts = [
        f"SATELLITE_INFO clock={data.last_seen_date or 'unknown'}",
        f"status={status}",
        f"outside={_fmt(b.outside_temperature)}°C",
    ]
    for n in (1, 2, 3):
        z = data.zones[n]
        if z.mode is None and z.ambient_temperature is None and z.setpoint_temperature is None:
            continue
        parts.append(
            f"z{n}=mode:{_zone_mode_label(z)} amb:{_fmt(z.ambient_temperature)}°C setpoint:{_fmt(z.setpoint_temperature)}°C"
        )
    return " ".join(parts)


def _zone_mode_label(zone: object) -> str:
    mode = getattr(zone, "mode", None)
    if mode is None:
        return "unknown"
    label = mode.value
    auto_comfort = getattr(zone, "auto_comfort", None)
    if auto_comfort is not None:
        label += f"/{'comfort' if auto_comfort else 'reduced'}"
    if getattr(zone, "override", False):
        label += "/override"
    return label


def _init_read_addr(payload: bytes) -> int | None:
    if len(payload) >= 9:
        addr = int.from_bytes(payload[0:2], "big")
        if addr >= 0x7000:
            return addr
    return None


def _init_write_addr(payload: bytes) -> int | None:
    if len(payload) >= 9:
        addr = int.from_bytes(payload[4:6], "big")
        if addr >= 0x7000:
            return addr
    return None
