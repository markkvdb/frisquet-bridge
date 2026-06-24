"""Tests for internal data model enums and defaults."""

from __future__ import annotations

from frisquet_bridge.model import (
    BoilerData,
    BoilerStatus,
    DhwMode,
    ZoneMode,
)


def test_dhw_mode_from_byte_masks_reserved_bits() -> None:
    assert DhwMode.from_byte(0x88) == DhwMode.ECO
    assert DhwMode.from_byte(0xFF) is None


def test_dhw_mode_byte_values() -> None:
    assert DhwMode.MAX.byte == 0x00
    assert DhwMode.STOP.byte == 0x28
    assert DhwMode.parse("Eco Horaires") == DhwMode.ECO_SCHEDULE


def test_zone_mode_round_trip() -> None:
    assert ZoneMode.from_byte(0x06) == ZoneMode.COMFORT
    assert ZoneMode.AUTO.byte == 0x05
    assert ZoneMode.from_byte(0x99) is None
    assert ZoneMode.parse("Réduit") == ZoneMode.REDUCED
    assert ZoneMode.parse("Hors Gel") == ZoneMode.FROST


def test_boiler_status_from_byte() -> None:
    assert BoilerStatus.from_byte(0x08) == BoilerStatus.RUNNING
    assert BoilerStatus.from_byte(0x04) == BoilerStatus.HEATING_OFF
    assert BoilerStatus.from_byte(0x00) == BoilerStatus.STANDBY


def test_boiler_data_default_zones() -> None:
    data = BoilerData()
    assert set(data.zones.keys()) == {1, 2, 3}
    assert data.zones[1].zone == 1
