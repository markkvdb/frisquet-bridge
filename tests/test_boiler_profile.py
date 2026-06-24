"""Tests for simplified and central-boiler profile logic."""

from __future__ import annotations

import pytest

from frisquet_bridge.boiler_profile import (
    SIMPLE_DEFAULT_COMFORT_TEMPERATURE,
    SIMPLE_DEFAULT_FROST_TEMPERATURE,
    SIMPLE_DEFAULT_REDUCED_TEMPERATURE,
    apply_simple_command,
    central_boiler_consigne,
    simple_consigne,
    simple_target_temperature,
)
from frisquet_bridge.climate import HVAC_AUTO, HVAC_HEAT, PRESET_ECO
from frisquet_bridge.model import ZoneMode, ZoneState


def test_simple_command_rejects_auto_mode() -> None:
    zone = ZoneState(zone=1, mode=ZoneMode.COMFORT)

    with pytest.raises(ValueError, match="off and heat"):
        apply_simple_command(zone, hvac_mode=HVAC_AUTO)


def test_simple_command_never_keeps_auto_from_state() -> None:
    zone = ZoneState(zone=1, mode=ZoneMode.AUTO, comfort_temperature=20.0, reported_ambient=19.0)

    apply_simple_command(zone, hvac_mode=HVAC_HEAT)

    assert zone.mode == ZoneMode.COMFORT
    assert zone.auto_comfort is None
    assert zone.override is False
    assert simple_target_temperature(zone) == 20.0


def test_simple_command_updates_active_setpoint() -> None:
    zone = ZoneState(zone=1, mode=ZoneMode.COMFORT, reduced_temperature=17.0)

    apply_simple_command(zone, preset=PRESET_ECO)
    apply_simple_command(zone, target_temperature=16.5)

    assert zone.mode == ZoneMode.REDUCED
    assert zone.reduced_temperature == 16.5


def test_simple_target_temperature_uses_mode_defaults() -> None:
    zone = ZoneState(zone=1, mode=ZoneMode.FROST)
    assert simple_target_temperature(zone) == SIMPLE_DEFAULT_FROST_TEMPERATURE

    zone.mode = ZoneMode.REDUCED
    assert simple_target_temperature(zone) == SIMPLE_DEFAULT_REDUCED_TEMPERATURE

    zone.mode = ZoneMode.COMFORT
    assert simple_target_temperature(zone) == SIMPLE_DEFAULT_COMFORT_TEMPERATURE


def test_simple_command_initializes_missing_defaults_without_overwriting_existing_values() -> None:
    zone = ZoneState(zone=1, mode=ZoneMode.COMFORT, comfort_temperature=21.0)

    apply_simple_command(zone, preset=PRESET_ECO)

    assert zone.frost_temperature == SIMPLE_DEFAULT_FROST_TEMPERATURE
    assert zone.reduced_temperature == SIMPLE_DEFAULT_REDUCED_TEMPERATURE
    assert zone.comfort_temperature == 21.0


def test_simple_consigne_requires_ambient() -> None:
    zone = ZoneState(zone=1, mode=ZoneMode.AUTO)

    assert simple_consigne(zone) is None

    zone.reported_ambient = 18.5
    consigne = simple_consigne(zone)

    assert consigne is not None
    assert consigne.ambient == 18.5
    assert consigne.setpoint == SIMPLE_DEFAULT_COMFORT_TEMPERATURE
    assert zone.mode == ZoneMode.COMFORT


def test_simple_thermostat_sequence_for_manual_trial() -> None:
    zone = ZoneState(zone=1)

    apply_simple_command(zone, hvac_mode="off")
    apply_simple_command(zone, target_temperature=9.0)
    assert zone.mode == ZoneMode.FROST
    assert zone.frost_temperature == 9.0

    apply_simple_command(zone, hvac_mode="heat")
    apply_simple_command(zone, preset=PRESET_ECO)
    apply_simple_command(zone, target_temperature=19.0)
    assert zone.mode == ZoneMode.REDUCED
    assert zone.reduced_temperature == 19.0

    apply_simple_command(zone, preset="comfort")
    apply_simple_command(zone, target_temperature=21.0)
    zone.reported_ambient = 28.0
    consigne = simple_consigne(zone)

    assert consigne is not None
    assert zone.mode == ZoneMode.COMFORT
    assert zone.comfort_temperature == 21.0
    assert consigne.ambient == 28.0
    assert consigne.setpoint == 21.0


def test_central_boiler_consigne_demand_on_and_off() -> None:
    zone = ZoneState(
        zone=1,
        central_setpoint=20.0,
        central_demand=True,
        central_demand_on_delta=4.0,
        central_demand_off_margin=1.0,
    )

    on = central_boiler_consigne(zone)
    zone.central_demand = False
    off = central_boiler_consigne(zone)

    assert on is not None
    assert on.setpoint == 20.0
    assert on.ambient == 16.0
    assert off is not None
    assert off.setpoint == 20.0
    assert off.ambient == 21.0
    assert zone.mode == ZoneMode.COMFORT


def test_central_boiler_consigne_requires_all_ha_values() -> None:
    zone = ZoneState(zone=1, central_setpoint=20.0, central_demand=True)

    assert central_boiler_consigne(zone) is None
