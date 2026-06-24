"""Pure control logic for simplified and central-boiler satellite profiles."""

from __future__ import annotations

from dataclasses import dataclass

from frisquet_bridge.climate import HVAC_HEAT, HVAC_OFF, PRESET_COMFORT, PRESET_ECO, parse_hvac_mode, parse_preset
from frisquet_bridge.model import ZoneMode, ZoneState

SIMPLE_HVAC_MODES = (HVAC_OFF, HVAC_HEAT)
SIMPLE_PRESET_MODES = (PRESET_COMFORT, PRESET_ECO)
SIMPLE_DEFAULT_FROST_TEMPERATURE = 8.0
SIMPLE_DEFAULT_REDUCED_TEMPERATURE = 18.0
SIMPLE_DEFAULT_COMFORT_TEMPERATURE = 20.0


@dataclass(frozen=True, slots=True)
class Consigne:
    ambient: float
    setpoint: float


def simple_hvac_mode(zone: ZoneState) -> str | None:
    if zone.mode is None:
        return None
    if zone.mode == ZoneMode.FROST:
        return HVAC_OFF
    return HVAC_HEAT


def simple_preset(zone: ZoneState) -> str | None:
    if zone.mode is None:
        return None
    if zone.mode == ZoneMode.REDUCED:
        return PRESET_ECO
    return PRESET_COMFORT


def simple_target_temperature(zone: ZoneState) -> float | None:
    mode = _simple_mode(zone)
    if mode == ZoneMode.FROST:
        return _temperature(zone.frost_temperature, SIMPLE_DEFAULT_FROST_TEMPERATURE)
    if mode == ZoneMode.REDUCED:
        return _temperature(zone.reduced_temperature, SIMPLE_DEFAULT_REDUCED_TEMPERATURE)
    return _temperature(zone.comfort_temperature, SIMPLE_DEFAULT_COMFORT_TEMPERATURE)


def apply_simple_command(
    zone: ZoneState,
    *,
    hvac_mode: str | None = None,
    preset: str | None = None,
    target_temperature: float | None = None,
) -> None:
    mode = _simple_mode(zone)
    if hvac_mode is not None:
        hvac = parse_hvac_mode(hvac_mode)
        if hvac not in SIMPLE_HVAC_MODES:
            raise ValueError("simple satellite supports only off and heat HVAC modes")
        if hvac == HVAC_OFF:
            mode = ZoneMode.FROST
        elif mode == ZoneMode.FROST:
            mode = ZoneMode.COMFORT

    if preset is not None:
        value = parse_preset(preset)
        if value not in SIMPLE_PRESET_MODES:
            raise ValueError("simple satellite supports only comfort and eco presets")
        if mode != ZoneMode.FROST:
            mode = ZoneMode.COMFORT if value == PRESET_COMFORT else ZoneMode.REDUCED

    zone.mode = mode
    zone.boost = False
    zone.override = False
    zone.auto_comfort = None

    if target_temperature is None:
        _ensure_simple_temperatures(zone)
        return
    temp = round(float(target_temperature), 1)
    if mode == ZoneMode.FROST:
        zone.frost_temperature = temp
    elif mode == ZoneMode.REDUCED:
        zone.reduced_temperature = temp
    else:
        zone.comfort_temperature = temp


def simple_consigne(zone: ZoneState) -> Consigne | None:
    ambient = zone.reported_ambient if zone.reported_ambient is not None else zone.ambient_temperature
    setpoint = simple_target_temperature(zone)
    if ambient is None or setpoint is None:
        return None
    mode = _simple_mode(zone)
    zone.mode = mode
    zone.boost = False
    zone.override = False
    zone.auto_comfort = None
    _ensure_simple_temperatures(zone)
    return Consigne(ambient=ambient, setpoint=setpoint)


def central_boiler_consigne(zone: ZoneState) -> Consigne | None:
    if (
        zone.central_setpoint is None
        or zone.central_demand is None
        or zone.central_demand_on_delta is None
        or zone.central_demand_off_margin is None
    ):
        return None

    setpoint = zone.central_setpoint
    ambient = (
        setpoint - zone.central_demand_on_delta
        if zone.central_demand
        else setpoint + zone.central_demand_off_margin
    )
    zone.mode = ZoneMode.COMFORT
    zone.boost = False
    zone.override = False
    zone.auto_comfort = None
    return Consigne(ambient=ambient, setpoint=setpoint)


def _simple_mode(zone: ZoneState) -> ZoneMode:
    if zone.mode in (ZoneMode.FROST, ZoneMode.REDUCED, ZoneMode.COMFORT):
        return zone.mode
    return ZoneMode.COMFORT


def _ensure_simple_temperatures(zone: ZoneState) -> None:
    if zone.frost_temperature is None:
        zone.frost_temperature = SIMPLE_DEFAULT_FROST_TEMPERATURE
    if zone.reduced_temperature is None:
        zone.reduced_temperature = SIMPLE_DEFAULT_REDUCED_TEMPERATURE
    if zone.comfort_temperature is None:
        zone.comfort_temperature = SIMPLE_DEFAULT_COMFORT_TEMPERATURE


def _temperature(value: float | None, default: float) -> float:
    return default if value is None else value
