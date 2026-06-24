"""Tests for zone climate mapping."""

from __future__ import annotations

import pytest

from frisquet_bridge.climate import (
    HVAC_AUTO,
    HVAC_HEAT,
    HVAC_OFF,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
    ClimateError,
    resolve_zone_intent,
    zone_hvac_action,
    zone_hvac_mode,
    zone_preset,
    zone_target_temperature,
)
from frisquet_bridge.model import BoilerState, ZoneMode, ZoneState


def _zone(**kwargs: object) -> ZoneState:
    base = ZoneState(zone=1)
    for key, value in kwargs.items():
        setattr(base, key, value)
    return base


def test_zone_hvac_mode_mapping() -> None:
    assert zone_hvac_mode(_zone(mode=ZoneMode.FROST)) == HVAC_OFF
    assert zone_hvac_mode(_zone(mode=ZoneMode.AUTO)) == HVAC_AUTO
    assert zone_hvac_mode(_zone(mode=ZoneMode.COMFORT)) == HVAC_HEAT
    assert zone_hvac_mode(_zone(mode=ZoneMode.REDUCED)) == HVAC_HEAT


def test_zone_preset_auto_falls_back_to_flags_when_setpoint_unknown() -> None:
    assert zone_preset(_zone(mode=ZoneMode.AUTO, override=False)) == PRESET_NONE
    assert zone_preset(_zone(mode=ZoneMode.AUTO, override=True, auto_comfort=True)) == PRESET_COMFORT
    assert zone_preset(_zone(mode=ZoneMode.AUTO, override=True, auto_comfort=False)) == PRESET_ECO


def test_zone_preset_auto_derived_from_live_setpoint() -> None:
    # No derogation: the displayed preset tracks the active program slot via the
    # boiler-reported setpoint.
    comfort_slot = _zone(
        mode=ZoneMode.AUTO,
        override=False,
        comfort_temperature=20.0,
        reduced_temperature=17.0,
        setpoint_temperature=20.0,
    )
    eco_slot = _zone(
        mode=ZoneMode.AUTO,
        override=False,
        comfort_temperature=20.0,
        reduced_temperature=17.0,
        setpoint_temperature=17.0,
    )
    assert zone_preset(comfort_slot) == PRESET_COMFORT
    assert zone_preset(eco_slot) == PRESET_ECO


def test_zone_preset_frost_is_none() -> None:
    assert zone_preset(_zone(mode=ZoneMode.FROST)) == PRESET_NONE


def test_zone_preset_heat_and_boost() -> None:
    assert zone_preset(_zone(mode=ZoneMode.COMFORT, boost=False)) == PRESET_COMFORT
    assert zone_preset(_zone(mode=ZoneMode.COMFORT, boost=True)) == PRESET_BOOST
    assert zone_preset(_zone(mode=ZoneMode.REDUCED)) == PRESET_ECO


def test_zone_hvac_action() -> None:
    boiler = BoilerState()
    assert zone_hvac_action(_zone(mode=ZoneMode.FROST), boiler) == "off"
    assert (
        zone_hvac_action(
            _zone(mode=ZoneMode.AUTO, ambient_temperature=18.0, setpoint_temperature=20.0),
            boiler,
        )
        == "heating"
    )
    assert (
        zone_hvac_action(
            _zone(mode=ZoneMode.AUTO, ambient_temperature=21.0, setpoint_temperature=20.0),
            boiler,
        )
        == "idle"
    )


def test_zone_target_temperature() -> None:
    assert zone_target_temperature(_zone(mode=ZoneMode.COMFORT, comfort_temperature=20.5)) == 20.5
    assert zone_target_temperature(_zone(mode=ZoneMode.COMFORT, boost=True, comfort_temperature=20.0)) == 22.0
    assert zone_target_temperature(_zone(mode=ZoneMode.REDUCED, reduced_temperature=17.0)) == 17.0


def test_resolve_auto_comfort_override() -> None:
    intent = resolve_zone_intent(_zone(mode=ZoneMode.AUTO), preset=PRESET_COMFORT)
    assert intent.mode == ZoneMode.AUTO
    assert intent.override is True
    assert intent.auto_comfort is True
    assert intent.boost is False


def test_resolve_auto_none_clears_override() -> None:
    intent = resolve_zone_intent(
        _zone(mode=ZoneMode.AUTO, override=True, auto_comfort=True),
        hvac_mode=HVAC_AUTO,
        preset=PRESET_NONE,
    )
    assert intent.mode == ZoneMode.AUTO
    assert intent.override is False


def test_resolve_heat_boost() -> None:
    intent = resolve_zone_intent(_zone(mode=ZoneMode.COMFORT), hvac_mode=HVAC_HEAT, preset=PRESET_BOOST)
    assert intent.mode == ZoneMode.COMFORT
    assert intent.boost is True


def test_resolve_off_frost() -> None:
    intent = resolve_zone_intent(_zone(mode=ZoneMode.AUTO), hvac_mode=HVAC_OFF)
    assert intent.mode == ZoneMode.FROST
    assert intent.boost is False


def test_resolve_target_updates_comfort_setpoint() -> None:
    intent = resolve_zone_intent(
        _zone(mode=ZoneMode.COMFORT, comfort_temperature=20.0, reduced_temperature=17.0, frost_temperature=7.0),
        target_temperature=21.5,
    )
    assert intent.comfort_temperature == 21.5


def test_resolve_boost_in_auto_raises() -> None:
    with pytest.raises(ClimateError, match="auto"):
        resolve_zone_intent(_zone(mode=ZoneMode.AUTO), preset=PRESET_BOOST)
