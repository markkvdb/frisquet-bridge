"""Tests for learned zone-state persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from frisquet_bridge.climate import zone_target_temperature
from frisquet_bridge.connect.decode import encode_satellite_mode
from frisquet_bridge.model import SCHEDULE_DAYS, BoilerData, ZoneMode, ZoneSchedule
from frisquet_bridge.state_store import load_protocol_request_ids, load_zone_state, save_protocol_request_id, save_zone_state

SCHEDULE_HEX = (
    "00e0ffffffff00e0ffffff7f00e0ffffff7f00e0ffffff7f00e0ffffff7f00e0ffffff7f00e0ffffffff"
)


def _seed_zone(data: BoilerData, zone: int = 1) -> None:
    zs = data.zones[zone]
    zs.mode = ZoneMode.COMFORT
    zs.mode_options = 0x24
    zs.comfort_temperature = 20.0
    zs.reduced_temperature = 17.0
    zs.frost_temperature = 7.0
    zs.schedule = ZoneSchedule.decode(bytes.fromhex(SCHEDULE_HEX))


def test_zone_state_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.state.json"
    src = BoilerData()
    _seed_zone(src, 1)

    save_zone_state(path, src)
    dst = BoilerData()
    load_zone_state(path, dst)

    z = dst.zones[1]
    assert z.mode == ZoneMode.COMFORT
    assert z.mode_options == 0x24
    assert z.comfort_temperature == 20.0
    assert z.reduced_temperature == 17.0
    assert z.frost_temperature == 7.0
    assert z.schedule is not None
    assert b"".join(z.schedule.days[d] for d in SCHEDULE_DAYS).hex() == SCHEDULE_HEX
    # Zones without learned config are not persisted.
    assert dst.zones[2].schedule is None


def test_central_state_round_trip_without_zone_schedule(tmp_path: Path) -> None:
    path = tmp_path / "config.state.json"
    src = BoilerData()
    zone = src.zones[1]
    zone.central_setpoint = 20.0
    zone.central_demand = True
    zone.central_demand_on_delta = 4.0
    zone.central_demand_off_margin = 1.0

    save_zone_state(path, src)
    dst = BoilerData()
    load_zone_state(path, dst)

    loaded = dst.zones[1]
    assert loaded.central_setpoint == 20.0
    assert loaded.central_demand is True
    assert loaded.central_demand_on_delta == 4.0
    assert loaded.central_demand_off_margin == 1.0


def test_zone_state_persists_partial_setpoints(tmp_path: Path) -> None:
    path = tmp_path / "config.state.json"
    src = BoilerData()
    src.zones[1].reduced_temperature = 16.5

    save_zone_state(path, src)
    dst = BoilerData()
    load_zone_state(path, dst)

    assert dst.zones[1].reduced_temperature == 16.5


@pytest.mark.parametrize(
    ("auto_comfort", "override", "wire_mode"),
    (
        (True, False, 0x05),
        (False, True, 0x02),
        (True, True, 0x03),
    ),
)
def test_auto_mode_flags_survive_restart_without_other_zone_metadata(
    tmp_path: Path,
    auto_comfort: bool,
    override: bool,
    wire_mode: int,
) -> None:
    path = tmp_path / "config.state.json"
    src = BoilerData()
    zone = src.zones[1]
    zone.mode = ZoneMode.AUTO
    zone.auto_comfort = auto_comfort
    zone.override = override

    save_zone_state(path, src)
    dst = BoilerData()
    load_zone_state(path, dst)

    loaded = dst.zones[1]
    assert loaded.mode == ZoneMode.AUTO
    assert loaded.auto_comfort is auto_comfort
    assert loaded.override is override
    assert encode_satellite_mode(loaded) == wire_mode


def test_boost_survives_restart_and_preserves_active_setpoint(tmp_path: Path) -> None:
    path = tmp_path / "config.state.json"
    src = BoilerData()
    zone = src.zones[1]
    zone.mode = ZoneMode.COMFORT
    zone.boost = True
    zone.comfort_temperature = 20.0

    save_zone_state(path, src)
    dst = BoilerData()
    load_zone_state(path, dst)

    loaded = dst.zones[1]
    assert loaded.mode == ZoneMode.COMFORT
    assert loaded.boost is True
    assert encode_satellite_mode(loaded) == 0x01
    assert zone_target_temperature(loaded) == pytest.approx(22.0)


def test_legacy_zone_state_without_mode_flags_keeps_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.state.json"
    path.write_text(
        '{"1": {"mode": "Auto", "mode_options": 16, "comfort_temperature": 20.0}}',
        encoding="utf-8",
    )

    data = BoilerData()
    load_zone_state(path, data)

    zone = data.zones[1]
    assert zone.mode == ZoneMode.AUTO
    assert zone.auto_comfort is None
    assert zone.override is False
    assert zone.boost is False


def test_load_missing_file_is_noop(tmp_path: Path) -> None:
    data = BoilerData()
    load_zone_state(tmp_path / "absent.state.json", data)
    assert data.zones[1].schedule is None


def test_load_ignores_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "config.state.json"
    path.write_text("{not valid json", encoding="utf-8")
    data = BoilerData()
    load_zone_state(path, data)
    assert data.zones[1].schedule is None


def test_protocol_request_id_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.state.json"

    save_protocol_request_id(path, "satellite_z1", 0x44)

    assert load_protocol_request_ids(path) == {"satellite_z1": 0x44}


def test_zone_state_preserves_protocol_request_ids(tmp_path: Path) -> None:
    path = tmp_path / "config.state.json"
    save_protocol_request_id(path, "satellite_z1", 0x44)
    data = BoilerData()
    data.zones[1].comfort_temperature = 20.0

    save_zone_state(path, data)

    assert load_protocol_request_ids(path) == {"satellite_z1": 0x44}
