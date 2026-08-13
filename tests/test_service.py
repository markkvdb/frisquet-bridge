"""Tests for service-level polling decisions."""

import asyncio
from pathlib import Path

import pytest

from frisquet_bridge.config import BridgeConfig, ConnectConfig, DeviceIdentity, ZoneConfig
from frisquet_bridge.model import BoilerData, ZoneSource
from frisquet_bridge.service import BridgeService, _configure_rf_diagnostics, _configure_zone_sources, _has_read_only_satellite_zone
from frisquet_bridge.transport.base import TransportError


async def test_diagnostics_loop_republishes_state(monkeypatch: pytest.MonkeyPatch) -> None:
    published = 0

    class Adapter:
        async def publish_state(self, _client: object) -> None:
            nonlocal published
            published += 1

    async def one_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", one_sleep)
    with pytest.raises(asyncio.CancelledError):
        await BridgeService._publish_diagnostics(Adapter(), object(), interval=30.0)  # type: ignore[arg-type]

    assert published == 1


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


def test_configure_zone_sources_maps_connect_physical_and_virtual_owners(tmp_path: Path) -> None:
    cfg = _config(tmp_path, "disabled", "satellite", "virtual_satellite")
    cfg.connect = None
    data = BoilerData()
    data.zones[1].source = ZoneSource.CONNECT

    _configure_zone_sources(cfg, data)

    assert data.zones[1].source == ZoneSource.CONNECT
    assert data.zones[2].source == ZoneSource.SATELLITE
    assert data.zones[3].source == ZoneSource.VIRTUAL


async def test_wait_for_stop_propagates_transport_failure() -> None:
    service = BridgeService.__new__(BridgeService)
    service._stop = asyncio.Event()

    async def fail_transport() -> None:
        raise TransportError("serial reader failed: disconnected")

    with pytest.raises(TransportError, match="disconnected"):
        await service._wait_for_stop_or_transport_failure(fail_transport())


async def test_offline_publish_failure_does_not_replace_transport_failure() -> None:
    service = BridgeService.__new__(BridgeService)
    failure = TransportError("serial reader failed: disconnected")

    class FailingAdapter:
        async def publish_offline(self, _client: object) -> None:
            raise RuntimeError("broker disconnected")

    await service._publish_offline_preserving_failure(FailingAdapter(), object(), failure)


def test_configure_zone_sources_maps_central_boiler_to_virtual_owner(tmp_path: Path) -> None:
    cfg = _config(tmp_path, "central_boiler", "disabled", "disabled")
    data = BoilerData()

    _configure_zone_sources(cfg, data)

    assert data.zones[1].source == ZoneSource.VIRTUAL


def test_rf_diagnostics_skip_one_shot_satellite_info_and_disabled_zone(tmp_path: Path) -> None:
    cfg = _config(tmp_path, "disabled", "virtual_satellite", "disabled")
    cfg.connect = ConnectConfig(mode="full", identity=DeviceIdentity(association_id=1, request_id=1))
    data = BoilerData()

    _configure_rf_diagnostics(cfg, data)

    assert "connect_satellite_info" not in data.rf_operations
    assert "connect_zone1_write" not in data.rf_operations


def test_rf_diagnostics_include_recurring_physical_satellite_info(tmp_path: Path) -> None:
    cfg = _config(tmp_path, "satellite", "disabled", "disabled")
    cfg.connect = ConnectConfig(mode="read", identity=DeviceIdentity(association_id=1, request_id=1))
    data = BoilerData()

    _configure_rf_diagnostics(cfg, data)

    assert "connect_satellite_info" in data.rf_operations
