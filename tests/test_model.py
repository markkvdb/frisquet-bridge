"""Tests for internal data model enums and defaults."""

from __future__ import annotations

from frisquet_bridge.model import (
    BoilerData,
    BoilerStatus,
    DhwMode,
    RfHealth,
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


def test_rf_health_tracks_outcomes_and_freshness_with_fake_clock() -> None:
    now = 100.0
    health = RfHealth(freshness_seconds=30.0, clock=lambda: now)

    health.record_success(rssi=-57)
    assert health.attempts == 1
    assert health.successes == 1
    assert health.consecutive_failures == 0
    assert health.last_rssi == -57
    assert health.is_fresh is True

    now = 131.0
    assert health.is_fresh is False
    health.record_nack()
    health.record_timeout()
    health.record_transport_failure()
    assert (health.nacks, health.timeouts, health.transport_failures) == (1, 1, 1)
    assert health.consecutive_failures == 3

    health.record_success(rssi=-49)
    assert health.consecutive_failures == 0
    assert health.last_rssi == -49


def test_rf_health_operations_are_independent() -> None:
    data = BoilerData()
    sensors = data.rf_health("connect_sensors", freshness_seconds=60.0)
    clock = data.rf_health("connect_clock", freshness_seconds=7200.0)

    sensors.record_timeout()
    clock.record_success(rssi=-65)

    assert sensors.timeouts == 1
    assert sensors.successes == 0
    assert clock.successes == 1
    assert clock.timeouts == 0


def test_boiler_data_default_zones() -> None:
    data = BoilerData()
    assert set(data.zones.keys()) == {1, 2, 3}
    assert data.zones[1].zone == 1
