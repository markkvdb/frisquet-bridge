"""Home Assistant climate mapping for Frisquet heating zones (pure logic, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass

from frisquet_bridge.model import BoilerState, ZoneMode, ZoneState

HVAC_OFF = "off"
HVAC_AUTO = "auto"
HVAC_HEAT = "heat"
HVAC_MODES = (HVAC_OFF, HVAC_AUTO, HVAC_HEAT)

# "none" is Home Assistant's reserved preset (always offered for a climate
# entity that advertises presets). We use it to mean "follow the weekly
# program" with no derogation, which removes the redundant custom "schedule"
# preset that used to sit next to HA's built-in "none".
PRESET_NONE = "none"
PRESET_COMFORT = "comfort"
PRESET_ECO = "eco"
PRESET_BOOST = "boost"
# Presets advertised to HA; it adds "none" itself, so we must not list it here.
PRESET_MODES = (PRESET_COMFORT, PRESET_ECO, PRESET_BOOST)
_VALID_PRESETS = (PRESET_NONE, *PRESET_MODES)

ACTION_HEATING = "heating"
ACTION_IDLE = "idle"
ACTION_OFF = "off"


class ClimateError(ValueError):
    """Invalid climate command or state."""


@dataclass(frozen=True, slots=True)
class ZoneIntent:
    mode: ZoneMode
    boost: bool
    override: bool
    auto_comfort: bool | None
    comfort_temperature: float | None = None
    reduced_temperature: float | None = None
    frost_temperature: float | None = None


def parse_hvac_mode(value: str) -> str:
    normalized = value.strip().casefold()
    if normalized not in HVAC_MODES:
        choices = ", ".join(HVAC_MODES)
        raise ClimateError(f"unknown hvac mode {value!r}; expected one of: {choices}")
    return normalized


def parse_preset(value: str) -> str:
    normalized = value.strip().casefold()
    if normalized not in _VALID_PRESETS:
        choices = ", ".join(_VALID_PRESETS)
        raise ClimateError(f"unknown preset {value!r}; expected one of: {choices}")
    return normalized


def zone_hvac_mode(zone: ZoneState) -> str | None:
    if zone.mode is None:
        return None
    if zone.mode == ZoneMode.FROST:
        return HVAC_OFF
    if zone.mode == ZoneMode.AUTO:
        return HVAC_AUTO
    return HVAC_HEAT


def zone_preset(zone: ZoneState) -> str | None:
    if zone.mode is None:
        return None
    if zone.mode == ZoneMode.FROST:
        # HVAC mode is "off"; no derogation applies.
        return PRESET_NONE
    if zone.mode == ZoneMode.AUTO:
        # In auto we surface the level the program (or an active derogation) is
        # currently applying, so the card shows comfort/eco that matches the
        # schedule slot rather than a static "schedule" label.
        return _auto_active_preset(zone)
    if zone.mode == ZoneMode.COMFORT:
        return PRESET_BOOST if zone.boost else PRESET_COMFORT
    if zone.mode == ZoneMode.REDUCED:
        return PRESET_ECO
    return None


def _auto_active_preset(zone: ZoneState) -> str:
    """Comfort/eco currently in effect for an auto zone.

    Prefers the boiler-reported active setpoint (which follows the weekly
    program in real time); falls back to the derogation flags, and finally to
    "none" when nothing is known yet.
    """
    setpoint = zone.setpoint_temperature
    comfort = zone.comfort_temperature
    reduced = zone.reduced_temperature
    if setpoint is not None and comfort is not None and reduced is not None and abs(comfort - reduced) > 0.2:
        return PRESET_COMFORT if abs(setpoint - comfort) <= abs(setpoint - reduced) else PRESET_ECO
    if zone.override:
        return PRESET_COMFORT if zone.auto_comfort else PRESET_ECO
    if zone.auto_comfort is True:
        return PRESET_COMFORT
    if zone.auto_comfort is False:
        return PRESET_ECO
    return PRESET_NONE


def zone_hvac_action(zone: ZoneState, boiler: BoilerState) -> str | None:
    if zone.mode is None:
        return None
    if zone.mode == ZoneMode.FROST:
        return ACTION_OFF
    ambient = zone.ambient_temperature
    setpoint = zone.setpoint_temperature
    if ambient is None or setpoint is None:
        if boiler.status is not None and boiler.status.value == "Running":
            return ACTION_HEATING
        return ACTION_IDLE
    if setpoint > ambient:
        return ACTION_HEATING
    return ACTION_IDLE


def zone_target_temperature(zone: ZoneState) -> float | None:
    if zone.mode is None:
        return None
    if zone.mode == ZoneMode.FROST:
        return zone.frost_temperature
    if zone.mode == ZoneMode.COMFORT:
        base = zone.comfort_temperature
        if zone.boost and base is not None:
            return base + 2.0
        return base
    if zone.mode == ZoneMode.REDUCED:
        return zone.reduced_temperature
    # AUTO: follow the boiler's live setpoint when known (tracks the program /
    # any derogation); otherwise fall back to the relevant fixed setpoint.
    if zone.setpoint_temperature is not None:
        return zone.setpoint_temperature
    if zone.auto_comfort is False:
        return zone.reduced_temperature
    return zone.comfort_temperature


def resolve_zone_intent(
    zone: ZoneState,
    *,
    hvac_mode: str | None = None,
    preset: str | None = None,
    target_temperature: float | None = None,
) -> ZoneIntent:
    """Reconcile HA climate command(s) against current zone state."""
    current_hvac = zone_hvac_mode(zone) or HVAC_AUTO
    current_preset = zone_preset(zone) or PRESET_NONE

    hvac = parse_hvac_mode(hvac_mode) if hvac_mode is not None else current_hvac
    preset_value = parse_preset(preset) if preset is not None else current_preset

    mode, boost, override, auto_comfort = _resolve_mode_flags(hvac, preset_value, zone)

    comfort = zone.comfort_temperature
    reduced = zone.reduced_temperature
    frost = zone.frost_temperature

    if target_temperature is not None:
        comfort, reduced, frost = _apply_target_temperature(
            target_temperature,
            mode=mode,
            boost=boost,
            override=override,
            auto_comfort=auto_comfort,
            comfort=comfort,
            reduced=reduced,
            frost=frost,
        )

    if boost and mode != ZoneMode.COMFORT:
        raise ClimateError("boost preset requires comfort mode")

    return ZoneIntent(
        mode=mode,
        boost=boost,
        override=override,
        auto_comfort=auto_comfort,
        comfort_temperature=comfort,
        reduced_temperature=reduced,
        frost_temperature=frost,
    )


def apply_zone_intent(zone: ZoneState, intent: ZoneIntent) -> None:
    """Apply a resolved intent to zone state in place (no I/O).

    Used for virtual-satellite zones, where there is no Connect write whose ACK
    would otherwise refresh the model.
    """
    zone.mode = intent.mode
    zone.boost = intent.boost
    zone.override = intent.override if intent.mode == ZoneMode.AUTO else False
    zone.auto_comfort = intent.auto_comfort if intent.mode == ZoneMode.AUTO else None
    if intent.comfort_temperature is not None:
        zone.comfort_temperature = intent.comfort_temperature
    if intent.reduced_temperature is not None:
        zone.reduced_temperature = intent.reduced_temperature
    if intent.frost_temperature is not None:
        zone.frost_temperature = intent.frost_temperature


def _resolve_mode_flags(
    hvac: str,
    preset: str,
    zone: ZoneState,
) -> tuple[ZoneMode, bool, bool, bool | None]:
    if hvac == HVAC_OFF:
        return ZoneMode.FROST, False, False, None

    if hvac == HVAC_AUTO:
        # "none" = follow the program (clear any derogation); comfort/eco set a
        # derogation to that level until the next program transition.
        if preset == PRESET_NONE:
            return ZoneMode.AUTO, False, False, zone.auto_comfort
        if preset == PRESET_COMFORT:
            return ZoneMode.AUTO, False, True, True
        if preset == PRESET_ECO:
            return ZoneMode.AUTO, False, True, False
        if preset == PRESET_BOOST:
            raise ClimateError("boost preset is not valid in auto mode")
        return ZoneMode.AUTO, False, False, zone.auto_comfort

    # heat
    if preset == PRESET_BOOST:
        return ZoneMode.COMFORT, True, False, None
    if preset == PRESET_COMFORT:
        return ZoneMode.COMFORT, False, False, None
    if preset == PRESET_ECO:
        return ZoneMode.REDUCED, False, False, None
    if preset == PRESET_NONE:
        return ZoneMode.AUTO, False, False, zone.auto_comfort
    return ZoneMode.COMFORT, False, False, None


def _apply_target_temperature(
    temperature: float,
    *,
    mode: ZoneMode,
    boost: bool,
    override: bool,
    auto_comfort: bool | None,
    comfort: float | None,
    reduced: float | None,
    frost: float | None,
) -> tuple[float | None, float | None, float | None]:
    temp = round(float(temperature), 1)
    if mode == ZoneMode.FROST:
        return comfort, reduced, temp
    if mode == ZoneMode.REDUCED:
        return comfort, temp, frost
    if mode == ZoneMode.COMFORT:
        return temp - (2.0 if boost else 0.0), reduced, frost
    if mode == ZoneMode.AUTO:
        if override:
            if auto_comfort:
                return temp, reduced, frost
            return comfort, temp, frost
        if auto_comfort is True:
            return temp, reduced, frost
        if auto_comfort is False:
            return comfort, temp, frost
        return temp, reduced, frost
    return comfort, reduced, frost
