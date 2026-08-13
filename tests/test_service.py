"""Tests for service-level polling decisions."""

from pathlib import Path

from frisquet_bridge.config import BridgeConfig, ZoneConfig
from frisquet_bridge.service import _has_read_only_satellite_zone


def _config(tmp_path: Path, *modes: str) -> BridgeConfig:
    cfg = BridgeConfig(path=tmp_path / "config.toml", network_id=bytes.fromhex("05d97f78"), boiler_addr=0x80)
    for zone_number, mode in enumerate(modes, 1):
        setattr(cfg, f"zone{zone_number}", ZoneConfig(mode=mode))
    return cfg


def test_virtual_zones_only_need_satellite_info_bootstrap(tmp_path: Path) -> None:
    cfg = _config(tmp_path, "virtual_satellite", "simple_satellite", "disabled")
    assert not _has_read_only_satellite_zone(cfg, (1, 2))


def test_central_zone_only_needs_satellite_info_bootstrap(tmp_path: Path) -> None:
    cfg = _config(tmp_path, "central_boiler", "disabled", "disabled")
    assert not _has_read_only_satellite_zone(cfg, (1,))


def test_physical_satellite_zone_enables_slow_satellite_info_polling(tmp_path: Path) -> None:
    cfg = _config(tmp_path, "virtual_satellite", "satellite", "disabled")
    assert _has_read_only_satellite_zone(cfg, (1, 2))
