"""Internal data model - the single source of truth for boiler/zone state.

These dataclasses are populated by the protocol layer and observed by adapters
(e.g. the optional MQTT/HA adapter). The model has no I/O dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# Sentinel the boiler uses for "outside temperature not available".
OUTSIDE_TEMP_ABSENT = 129.0


class DhwMode(StrEnum):
    """Domestic hot water (DHW) mode. Values are the raw protocol bytes."""

    MAX = "Max"
    ECO = "Eco"
    ECO_SCHEDULE = "Eco Schedule"
    ECO_PLUS = "Eco+"
    ECO_PLUS_SCHEDULE = "Eco+ Schedule"
    STOP = "Stop"

    @property
    def byte(self) -> int:
        return _DHW_MODE_TO_BYTE[self]

    @classmethod
    def from_byte(cls, raw: int) -> DhwMode | None:
        return _DHW_BYTE_TO_MODE.get(raw & 0x7E)

    @classmethod
    def parse(cls, value: str) -> DhwMode:
        normalized = _normalize_label(value)
        mode = _DHW_LABELS.get(normalized)
        if mode is None:
            choices = ", ".join(m.value for m in cls)
            raise ValueError(f"unknown DHW mode {value!r}; expected one of: {choices}")
        return mode


_DHW_MODE_TO_BYTE: dict[DhwMode, int] = {
    DhwMode.MAX: 0x00,
    DhwMode.ECO: 0x08,
    DhwMode.ECO_SCHEDULE: 0x10,
    DhwMode.ECO_PLUS: 0x18,
    DhwMode.ECO_PLUS_SCHEDULE: 0x20,
    DhwMode.STOP: 0x28,
}
_DHW_BYTE_TO_MODE: dict[int, DhwMode] = {v: k for k, v in _DHW_MODE_TO_BYTE.items()}

# Modes exposed in the Home Assistant DHW selector. The Eco+ variants only
# exist on some boiler models (e.g. those with a dedicated DHW tank), so the
# selector advertises only the four modes the standard app shows. The full
# enum above is still used to *decode* whatever the boiler reports.
DHW_SELECTABLE_MODES: tuple[DhwMode, ...] = (
    DhwMode.MAX,
    DhwMode.ECO,
    DhwMode.ECO_SCHEDULE,
    DhwMode.STOP,
)


class ZoneMode(StrEnum):
    """Heating mode for a zone."""

    AUTO = "Auto"
    COMFORT = "Comfort"
    REDUCED = "Reduced"
    FROST = "Frost protection"

    @property
    def byte(self) -> int:
        return _ZONE_MODE_TO_BYTE[self]

    @classmethod
    def from_byte(cls, raw: int) -> ZoneMode | None:
        return _ZONE_BYTE_TO_MODE.get(raw)

    @classmethod
    def parse(cls, value: str) -> ZoneMode:
        normalized = _normalize_label(value)
        mode = _ZONE_LABELS.get(normalized)
        if mode is None:
            choices = ", ".join(m.value for m in cls)
            raise ValueError(f"unknown zone mode {value!r}; expected one of: {choices}")
        return mode


_ZONE_MODE_TO_BYTE: dict[ZoneMode, int] = {
    ZoneMode.AUTO: 0x05,
    ZoneMode.COMFORT: 0x06,
    ZoneMode.REDUCED: 0x07,
    ZoneMode.FROST: 0x08,
}
_ZONE_BYTE_TO_MODE: dict[int, ZoneMode] = {v: k for k, v in _ZONE_MODE_TO_BYTE.items()}


def _normalize_label(value: str) -> str:
    return value.strip().casefold().replace("é", "e").replace("_", " ").replace("-", " ")


_DHW_LABELS: dict[str, DhwMode] = {
    _normalize_label(mode.value): mode for mode in DhwMode
}
_DHW_LABELS.update(
    {
        "eco horaires": DhwMode.ECO_SCHEDULE,
        "eco schedule": DhwMode.ECO_SCHEDULE,
        "eco+ horaires": DhwMode.ECO_PLUS_SCHEDULE,
        "eco+ schedule": DhwMode.ECO_PLUS_SCHEDULE,
    }
)
_ZONE_LABELS: dict[str, ZoneMode] = {
    _normalize_label(mode.value): mode for mode in ZoneMode
}
_ZONE_LABELS.update(
    {
        "reduit": ZoneMode.REDUCED,
        "eco": ZoneMode.REDUCED,
        "hors gel": ZoneMode.FROST,
        "frost": ZoneMode.FROST,
    }
)


class BoilerStatus(StrEnum):
    STANDBY = "Standby"
    RUNNING = "Running"
    HEATING_OFF = "Heating off"

    @classmethod
    def from_byte(cls, status_byte: int) -> BoilerStatus:
        if status_byte & 0b0000_0100:
            return cls.HEATING_OFF
        if status_byte & 0b0000_1000:
            return cls.RUNNING
        return cls.STANDBY


SCHEDULE_DAYS = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")


@dataclass(frozen=True)
class ZoneSchedule:
    days: dict[str, bytes]

    @classmethod
    def decode(cls, payload: bytes) -> ZoneSchedule:
        if len(payload) < 42:
            raise ValueError(f"zone schedule payload too short: {len(payload)} bytes")
        return cls(
            days={
                day: payload[offset : offset + 6]
                for offset, day in zip(range(0, 42, 6), SCHEDULE_DAYS, strict=True)
            }
        )

    def compact_hex(self) -> str:
        return " ".join(f"{day[:3]}={self.days[day].hex()}" for day in SCHEDULE_DAYS)


@dataclass
class BoilerState:
    dhw_temperature: float | None = None
    dhw_instant_temperature: float | None = None
    cdc_temperature: float | None = None
    cdc_safety_temperature: float | None = None
    flue_temperature: float | None = None
    outside_temperature: float | None = None
    dhw_power: float | None = None
    heating_power: float | None = None
    pressure: float | None = None
    dhw_consumption: int | None = None
    heating_consumption: int | None = None
    daily_dhw_consumption: int | None = None
    daily_heating_consumption: int | None = None
    dhw_mode: DhwMode | None = None
    dhw_frame_bits: int = 0
    holiday_active: bool | None = None
    status: BoilerStatus | None = None
    fault: bool = False


@dataclass
class ZoneState:
    zone: int
    flow_temperature: float | None = None
    flow_setpoint_temperature: float | None = None
    ambient_temperature: float | None = None
    setpoint_temperature: float | None = None
    # Room temperature we report when acting as a virtual satellite for this
    # zone (fed from Home Assistant). Distinct from ``ambient_temperature``,
    # which is what the boiler/satellite reports back to us.
    reported_ambient: float | None = None
    mode: ZoneMode | None = None
    mode_options: int | None = None
    auto_comfort: bool | None = None
    override: bool = False
    comfort_temperature: float | None = None
    reduced_temperature: float | None = None
    frost_temperature: float | None = None
    boost: bool = False
    schedule: ZoneSchedule | None = None
    central_setpoint: float | None = None
    central_demand: bool | None = None
    central_demand_on_delta: float | None = None
    central_demand_off_margin: float | None = None


@dataclass
class SondeState:
    outside_temperature: float | None = None


@dataclass
class BoilerData:
    """Aggregate state for the whole installation."""

    boiler: BoilerState = field(default_factory=BoilerState)
    zones: dict[int, ZoneState] = field(default_factory=lambda: {n: ZoneState(zone=n) for n in (1, 2, 3)})
    sonde: SondeState = field(default_factory=SondeState)
    last_seen_date: str | None = None
