"""Tests for TOML config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from frisquet_bridge.config import BridgeConfig, ConfigError, load


def test_load_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[frisquet.connect]
mode = "full"
association_id = "ea"

[frisquet.sonde]
enabled = false
association_id = "9c"

[frisquet.zone1]
mode = "satellite"

[frisquet.zone2]
mode = "virtual_satellite"
association_id = "c0"

[frisquet.zone3]
mode = "disabled"

[mqtt]
language = "fr"

[logging]
level = "DEBUG"
format = "json"
file = "logs/bridge.log"
file_max_bytes = 12345
file_backup_count = 2
raw_file = "logs/raw.jsonl"
raw_max_bytes = 54321
raw_backup_count = 3
raw_lines = true
""",
        encoding="utf-8",
    )

    cfg = load(path)

    assert cfg.network_id == bytes.fromhex("05d97f78")
    assert cfg.boiler_addr == 0x80
    assert cfg.sensor_poll_interval_seconds == 30.0
    assert cfg.connect is not None
    assert cfg.connect.mode == "full"
    assert cfg.connect.identity is not None
    assert cfg.connect.identity.association_id == 0xEA
    assert cfg.connect.identity.request_id == 0
    assert cfg.connect_reads_enabled is True
    assert cfg.connect_writes_enabled is True
    assert cfg.sonde is not None
    assert cfg.sonde.enabled is False
    assert cfg.sonde.association_id == 0x9C
    assert cfg.sonde.request_id == 0
    assert cfg.zone1 is not None
    assert cfg.zone1.mode == "satellite"
    assert cfg.zone2 is not None
    assert cfg.zone2.mode == "virtual_satellite"
    assert cfg.identity("satellite_z2").association_id == 0xC0
    assert cfg.zone3 is not None
    assert cfg.zone3.mode == "disabled"
    assert cfg.mqtt.language == "fr"
    assert cfg.logging.level == "DEBUG"
    assert cfg.logging.format == "json"
    assert cfg.logging.file == "logs/bridge.log"
    assert cfg.logging.file_max_bytes == 12345
    assert cfg.logging.file_backup_count == 2
    assert cfg.logging.raw_file == "logs/raw.jsonl"
    assert cfg.logging.raw_max_bytes == 54321
    assert cfg.logging.raw_backup_count == 3
    assert cfg.logging.raw_lines is True


def test_load_minimal_config_has_no_roles(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"
""",
        encoding="utf-8",
    )

    cfg = load(path)

    assert cfg.connect is None
    assert cfg.connect_reads_enabled is False
    assert cfg.connect_writes_enabled is False
    assert cfg.boiler_entities_enabled is False
    assert cfg.sonde is None
    assert cfg.zone1 is None
    assert cfg.mqtt.language == "en"


def test_load_rejects_invalid_mqtt_language(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[mqtt]
language = "de"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="mqtt.language"):
        load(path)


def test_load_satellite_zone_is_passive_without_identity(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[frisquet.zone1]
mode = "satellite"
""",
        encoding="utf-8",
    )

    cfg = load(path)

    assert cfg.zone1 is not None
    assert cfg.zone1.mode == "satellite"
    assert cfg.zone1.identity is None
    assert cfg.zone1.is_read_only_satellite is True


def test_load_sensor_poll_interval(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"
sensor_poll_interval_seconds = 12.5
""",
        encoding="utf-8",
    )

    cfg = load(path)

    assert cfg.sensor_poll_interval_seconds == 12.5


def test_load_rejects_invalid_sensor_poll_interval(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"
sensor_poll_interval_seconds = 0
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="sensor_poll_interval_seconds"):
        load(path)


def test_load_connect_read_mode_enables_reads_only(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[frisquet.connect]
mode = "read"
association_id = "ea"
""",
        encoding="utf-8",
    )

    cfg = load(path)

    assert cfg.connect is not None
    assert cfg.connect.mode == "read"
    assert cfg.connect_reads_enabled is True
    assert cfg.connect_writes_enabled is False
    assert cfg.connect_physical_zone_control_enabled is False


def test_load_connect_full_mode_enables_reads_and_writes(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[frisquet.connect]
mode = "full"
association_id = "ea"
""",
        encoding="utf-8",
    )

    cfg = load(path)

    assert cfg.connect_reads_enabled is True
    assert cfg.connect_writes_enabled is True
    assert cfg.connect_physical_zone_control_enabled is True


def test_load_connect_passive_mode_does_not_require_identity(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[frisquet.connect]
mode = "passive"
""",
        encoding="utf-8",
    )

    cfg = load(path)

    assert cfg.connect is not None
    assert cfg.connect.mode == "passive"
    assert cfg.connect.identity is None
    assert cfg.connect_reads_enabled is False
    assert cfg.connect_writes_enabled is False
    assert cfg.boiler_entities_enabled is True


def test_load_connect_read_requires_identity(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[frisquet.connect]
mode = "read"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="connect.association_id"):
        load(path)


def test_load_rejects_read_boiler(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"
read_boiler = true
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unknown field 'read_boiler'"):
        load(path)


def test_protocol_request_id_loads_from_state_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[frisquet.zone1]
mode = "simple_satellite"
association_id = "c0"
""",
        encoding="utf-8",
    )
    path.with_name("config.state.json").write_text(
        """
{
  "protocol": {
    "satellite_z1": {"request_id": 68}
  }
}
""",
        encoding="utf-8",
    )

    cfg = load(path)

    assert cfg.zone1 is not None
    assert cfg.zone1.identity is not None
    assert cfg.zone1.identity.association_id == 0xC0
    assert cfg.zone1.identity.request_id == 0x44


def test_load_rejects_request_id_in_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[frisquet.zone1]
mode = "simple_satellite"
association_id = "c0"
request_id = "00"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unknown field 'request_id'"):
        load(path)


def test_load_requires_satellite_identity_for_satellite_zone(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[frisquet.sonde]
enabled = false
association_id = "9c"

[frisquet.zone1]
mode = "virtual_satellite"

[frisquet.zone2]
mode = "disabled"

[frisquet.zone3]
mode = "disabled"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="zone1.association_id"):
        load(path)


def test_load_requires_identity_for_simple_satellite(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[frisquet.zone1]
mode = "simple_satellite"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="zone1.association_id"):
        load(path)


def test_load_central_boiler_only_allowed_on_zone1(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[frisquet.zone2]
mode = "central_boiler"
association_id = "c0"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="central_boiler is only valid"):
        load(path)


def test_load_central_boiler_rejects_other_enabled_zones(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[frisquet.zone1]
mode = "central_boiler"
association_id = "c0"

[frisquet.zone2]
mode = "satellite"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="requires zone2 to be disabled"):
        load(path)


def test_load_rejects_zone_temperatures_in_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[frisquet.zone1]
mode = "central_boiler"
association_id = "c0"
central_setpoint = 20.0
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unknown field 'central_setpoint'"):
        load(path)


def test_load_rejects_connect_enabled_field(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[frisquet.connect]
mode = "full"
enabled = true
association_id = "ea"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unknown field 'enabled'"):
        load(path)


def test_load_rejects_connect_passive_field(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[frisquet.connect]
mode = "full"
passive = false
association_id = "ea"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unknown field 'passive'"):
        load(path)


def test_load_rejects_connect_zone_mode(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[frisquet.zone1]
mode = "connect"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="frisquet.zone1.mode"):
        load(path)


def test_load_rejects_physical_satellite_zone_mode(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[frisquet.zone1]
mode = "physical_satellite"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="frisquet.zone1.mode"):
        load(path)


def test_load_rejects_unknown_top_level_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[home_assistant]
enabled = true
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unknown field 'home_assistant'"):
        load(path)


def test_load_rejects_invalid_logging_format(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"

[logging]
format = "pretty"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="logging.format"):
        load(path)


def test_save_writes_clean_nested_schema(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    cfg = BridgeConfig(path=path, network_id=bytes.fromhex("05d97f78"), boiler_addr=0x80)
    cfg.set_identity("connect", association_id=0xEA, request_id=0x48)
    cfg.set_identity("sonde", association_id=0x9C, request_id=0x18)

    cfg.save()
    text = path.read_text(encoding="utf-8")

    assert "[frisquet.connect]" in text
    assert "[frisquet.sonde]" in text
    assert "[frisquet.zone1]" not in text
    assert "request_id" not in text
    assert "[connect]" not in text
    assert "use_satellite_z1" not in text
    assert "[home_assistant]" not in text
    assert "[logging]" in text
    assert 'language = "en"' in text
