"""Tests for MQTT command mapping."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from frisquet_bridge.climate import HVAC_AUTO, PRESET_COMFORT
from frisquet_bridge.config import BridgeConfig, ConnectConfig, DeviceIdentity, ZoneConfig
from frisquet_bridge.model import BoilerData, BoilerStatus, DhwMode, ZoneMode
from frisquet_bridge.mqtt.adapter import MqttAdapter


class FakeOps:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def write_dhw_mode(self, data: BoilerData, mode: DhwMode) -> None:
        self.calls.append(("dhw", mode))
        data.boiler.dhw_mode = mode

    async def apply_zone_climate(
        self,
        zone: int,
        data: BoilerData,
        *,
        hvac_mode: str | None = None,
        preset: str | None = None,
        target_temperature: float | None = None,
    ) -> None:
        self.calls.append(("climate", zone, hvac_mode, preset, target_temperature))
        if hvac_mode == "auto":
            data.zones[zone].mode = ZoneMode.AUTO
        if preset == "comfort":
            data.zones[zone].override = True
            data.zones[zone].auto_comfort = True


class SlowDhwOps(FakeOps):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def write_dhw_mode(self, data: BoilerData, mode: DhwMode) -> None:
        self.started.set()
        await asyncio.wait_for(self.release.wait(), timeout=1.0)
        await super().write_dhw_mode(data, mode)


class FakeSatellite:
    def __init__(self) -> None:
        self.sends = 0

    async def send_now(self) -> bool:
        self.sends += 1
        return True


class FakeMqttClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, bool]] = []

    async def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        self.published.append((topic, payload, retain))


@pytest.fixture
def adapter(tmp_path: Path) -> tuple[MqttAdapter, BoilerData, FakeOps]:
    cfg = BridgeConfig(path=tmp_path / "config.toml", network_id=bytes.fromhex("05d97f78"), boiler_addr=0x80)
    cfg.connect = ConnectConfig(mode="full", identity=DeviceIdentity(association_id=0xEA, request_id=0x48))
    cfg.zone1 = ZoneConfig(mode="satellite")
    data = BoilerData()
    ops = FakeOps()
    return MqttAdapter(cfg, data, ops), data, ops


@pytest.fixture
def virtual_adapter(tmp_path: Path) -> tuple[MqttAdapter, BoilerData, FakeSatellite]:
    cfg = BridgeConfig(path=tmp_path / "config.toml", network_id=bytes.fromhex("05d97f78"), boiler_addr=0x80)
    cfg.zone1 = ZoneConfig(mode="virtual_satellite")
    data = BoilerData()
    satellite = FakeSatellite()
    adapter = MqttAdapter(cfg, data, None, virtual_satellites={1: satellite})  # type: ignore[dict-item]
    return adapter, data, satellite


@pytest.fixture
def satellite_adapter(tmp_path: Path) -> tuple[MqttAdapter, BoilerData, FakeOps]:
    cfg = BridgeConfig(path=tmp_path / "config.toml", network_id=bytes.fromhex("05d97f78"), boiler_addr=0x80)
    cfg.connect = ConnectConfig(mode="read", identity=DeviceIdentity(association_id=0xEA, request_id=0x48))
    cfg.zone1 = ZoneConfig(mode="satellite")
    data = BoilerData()
    ops = FakeOps()
    return MqttAdapter(cfg, data, ops), data, ops


@pytest.fixture
def simple_adapter(tmp_path: Path) -> tuple[MqttAdapter, BoilerData, FakeSatellite, list[str]]:
    cfg = BridgeConfig(path=tmp_path / "config.toml", network_id=bytes.fromhex("05d97f78"), boiler_addr=0x80)
    cfg.connect = ConnectConfig(mode="read", identity=DeviceIdentity(association_id=0xEA, request_id=0x48))
    cfg.zone1 = ZoneConfig(mode="simple_satellite")
    data = BoilerData()
    satellite = FakeSatellite()
    persisted: list[str] = []
    adapter = MqttAdapter(
        cfg,
        data,
        None,
        virtual_satellites={1: satellite},  # type: ignore[dict-item]
        on_persist_state=lambda: persisted.append("saved"),
    )
    return adapter, data, satellite, persisted


@pytest.fixture
def central_adapter(tmp_path: Path) -> tuple[MqttAdapter, BoilerData, FakeSatellite, list[str]]:
    cfg = BridgeConfig(path=tmp_path / "config.toml", network_id=bytes.fromhex("05d97f78"), boiler_addr=0x80)
    cfg.zone1 = ZoneConfig(mode="central_boiler")
    data = BoilerData()
    satellite = FakeSatellite()
    persisted: list[str] = []
    adapter = MqttAdapter(
        cfg,
        data,
        None,
        virtual_satellites={1: satellite},  # type: ignore[dict-item]
        on_persist_state=lambda: persisted.append("saved"),
    )
    return adapter, data, satellite, persisted


async def test_handle_dhw_command_accepts_openfrisquet_label(adapter: tuple[MqttAdapter, BoilerData, FakeOps]) -> None:
    mqtt, _data, ops = adapter

    await mqtt.handle_command("frisquet/boiler/dhwMode/set", "Eco Horaires")

    assert ops.calls == [("dhw", DhwMode.ECO_SCHEDULE)]


async def test_dhw_command_state_does_not_bounce_on_stale_read(adapter: tuple[MqttAdapter, BoilerData, FakeOps]) -> None:
    mqtt, data, ops = adapter
    client = FakeMqttClient()

    await mqtt.handle_command("frisquet/boiler/dhwMode/set", "Eco")
    data.boiler.dhw_mode = DhwMode.STOP

    await mqtt.publish_state(client)  # type: ignore[arg-type]
    await mqtt.handle_command("frisquet/boiler/dhwMode/set", "Eco")

    assert ("frisquet/boiler/dhwMode", "Eco", True) in client.published
    assert ops.calls == [("dhw", DhwMode.ECO)]


async def test_dhw_command_publishes_pending_state_before_rf_write(tmp_path: Path) -> None:
    cfg = BridgeConfig(path=tmp_path / "config.toml", network_id=bytes.fromhex("05d97f78"), boiler_addr=0x80)
    cfg.connect = ConnectConfig(mode="full", identity=DeviceIdentity(association_id=0xEA, request_id=0x48))
    data = BoilerData()
    data.boiler.dhw_mode = DhwMode.STOP
    ops = SlowDhwOps()
    client = FakeMqttClient()
    mqtt = MqttAdapter(
        cfg,
        data,
        ops,  # type: ignore[arg-type]
        on_state_change=lambda: mqtt.publish_state(client),  # type: ignore[name-defined, arg-type]
    )

    task = asyncio.create_task(mqtt.handle_command("frisquet/boiler/dhwMode/set", "Eco"))
    try:
        await asyncio.wait_for(ops.started.wait(), timeout=1.0)
        assert ("frisquet/boiler/dhwMode", "Eco", True) in client.published
        assert ops.calls == []
        ops.release.set()
        await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert ops.calls == [("dhw", DhwMode.ECO)]


async def test_handle_zone_mode_command(adapter: tuple[MqttAdapter, BoilerData, FakeOps]) -> None:
    mqtt, _data, ops = adapter

    await mqtt.handle_command("frisquet/z1/mode/set", HVAC_AUTO)

    assert ops.calls == [("climate", 1, HVAC_AUTO, None, None)]


async def test_handle_zone_preset_command(adapter: tuple[MqttAdapter, BoilerData, FakeOps]) -> None:
    mqtt, _data, ops = adapter

    await mqtt.handle_command("frisquet/z1/preset/set", PRESET_COMFORT)

    assert ops.calls == [("climate", 1, None, PRESET_COMFORT, None)]


async def test_handle_zone_target_temperature_command(adapter: tuple[MqttAdapter, BoilerData, FakeOps]) -> None:
    mqtt, _data, ops = adapter

    await mqtt.handle_command("frisquet/z1/targetTemperature/set", "21.0")

    assert ops.calls == [("climate", 1, None, None, 21.0)]


async def test_virtual_zone_reported_ambient_triggers_send(
    virtual_adapter: tuple[MqttAdapter, BoilerData, FakeSatellite],
) -> None:
    mqtt, data, satellite = virtual_adapter

    await mqtt.handle_command("frisquet/z1/reportedAmbient/set", "20.5")

    assert data.zones[1].reported_ambient == 20.5
    assert satellite.sends == 1


async def test_virtual_zone_mode_updates_state_and_sends(
    virtual_adapter: tuple[MqttAdapter, BoilerData, FakeSatellite],
) -> None:
    mqtt, data, satellite = virtual_adapter

    await mqtt.handle_command("frisquet/z1/mode/set", HVAC_AUTO)

    assert data.zones[1].mode == ZoneMode.AUTO
    assert satellite.sends == 1


async def test_satellite_zone_command_is_ignored(
    satellite_adapter: tuple[MqttAdapter, BoilerData, FakeOps],
) -> None:
    mqtt, _data, ops = satellite_adapter

    await mqtt.handle_command("frisquet/z1/mode/set", HVAC_AUTO)

    assert ops.calls == []


async def test_simple_zone_command_persists_and_sends(
    simple_adapter: tuple[MqttAdapter, BoilerData, FakeSatellite, list[str]],
) -> None:
    mqtt, data, satellite, persisted = simple_adapter

    await mqtt.handle_command("frisquet/z1/mode/set", "heat")
    await mqtt.handle_command("frisquet/z1/preset/set", "eco")
    await mqtt.handle_command("frisquet/z1/targetTemperature/set", "16.5")

    assert data.zones[1].mode == ZoneMode.REDUCED
    assert data.zones[1].reduced_temperature == 16.5
    assert satellite.sends == 3
    assert persisted == ["saved", "saved", "saved"]


async def test_central_commands_update_state_persist_and_send(
    central_adapter: tuple[MqttAdapter, BoilerData, FakeSatellite, list[str]],
) -> None:
    mqtt, data, satellite, persisted = central_adapter

    await mqtt.handle_command("frisquet/central/setpoint/set", "20.0")
    await mqtt.handle_command("frisquet/central/demandOnDelta/set", "4.0")
    await mqtt.handle_command("frisquet/central/demandOffMargin/set", "1.0")
    await mqtt.handle_command("frisquet/central/demand/set", "ON")

    zone = data.zones[1]
    assert zone.central_setpoint == 20.0
    assert zone.central_demand_on_delta == 4.0
    assert zone.central_demand_off_margin == 1.0
    assert zone.central_demand is True
    assert satellite.sends == 4
    assert persisted == ["saved", "saved", "saved", "saved"]


async def test_central_discovery_exposes_proxy_entities(
    central_adapter: tuple[MqttAdapter, BoilerData, FakeSatellite, list[str]],
) -> None:
    mqtt, _data, _satellite, _persisted = central_adapter
    client = FakeMqttClient()

    await mqtt.publish_discovery(client)  # type: ignore[arg-type]

    payloads = {topic: payload for topic, payload, _retain in client.published}
    assert payloads["homeassistant/switch/frisquet_bridge/central_heat_demand/config"]
    assert payloads["homeassistant/number/frisquet_bridge/central_setpoint/config"]
    assert payloads["homeassistant/number/frisquet_bridge/central_demand_on_delta/config"]
    assert payloads["homeassistant/number/frisquet_bridge/central_demand_off_margin/config"]
    assert payloads["homeassistant/climate/frisquet_bridge/zone1_climate/config"] == ""


async def test_satellite_only_discovery_keeps_boiler_sensors_and_dhw_select(
    simple_adapter: tuple[MqttAdapter, BoilerData, FakeSatellite, list[str]],
) -> None:
    mqtt, _data, _satellite, _persisted = simple_adapter
    client = FakeMqttClient()

    await mqtt.publish_discovery(client)  # type: ignore[arg-type]

    payloads = {topic: payload for topic, payload, _retain in client.published}
    assert payloads["homeassistant/sensor/frisquet_bridge/dhw_temperature/config"]
    dhw_mode = json.loads(payloads["homeassistant/sensor/frisquet_bridge/dhw_mode/config"])
    assert dhw_mode["state_topic"] == "frisquet/boiler/dhwMode"
    assert "command_topic" not in dhw_mode
    assert payloads["homeassistant/select/frisquet_bridge/dhw_mode/config"] == ""
    assert payloads["homeassistant/sensor/frisquet_bridge/zone1_schedule_raw/config"] == ""
    assert payloads["homeassistant/switch/frisquet_bridge/zone1_boost/config"] == ""
    assert "homeassistant/climate/frisquet_bridge/zone1_climate/config" in payloads


async def test_connect_discovery_exposes_dhw_mode_select(adapter: tuple[MqttAdapter, BoilerData, FakeOps]) -> None:
    mqtt, _data, _ops = adapter
    client = FakeMqttClient()

    await mqtt.publish_discovery(client)  # type: ignore[arg-type]

    payloads = {topic: payload for topic, payload, _retain in client.published}
    dhw_mode = json.loads(payloads["homeassistant/select/frisquet_bridge/dhw_mode/config"])
    assert dhw_mode["state_topic"] == "frisquet/boiler/dhwMode"
    assert dhw_mode["command_topic"] == "frisquet/boiler/dhwMode/set"
    assert payloads["homeassistant/sensor/frisquet_bridge/dhw_mode/config"] == ""


async def test_satellite_discovery_is_read_only(
    satellite_adapter: tuple[MqttAdapter, BoilerData, FakeOps],
) -> None:
    mqtt, _data, _ops = satellite_adapter
    client = FakeMqttClient()

    await mqtt.publish_discovery(client)  # type: ignore[arg-type]

    payloads = {topic: payload for topic, payload, _retain in client.published}
    climate = json.loads(payloads["homeassistant/climate/frisquet_bridge/zone1_climate/config"])
    assert climate["mode_state_topic"] == "frisquet/z1/mode"
    assert "mode_command_topic" not in climate
    assert "preset_mode_command_topic" not in climate
    assert "temperature_command_topic" not in climate


async def test_discovery_adds_availability_and_sensor_metadata(adapter: tuple[MqttAdapter, BoilerData, FakeOps]) -> None:
    mqtt, _data, _ops = adapter
    client = FakeMqttClient()

    await mqtt.publish_discovery(client)  # type: ignore[arg-type]

    payloads = {
        topic: json.loads(payload)
        for topic, payload, _retain in client.published
        if topic.startswith("homeassistant/") and payload
    }
    dhw_temperature = payloads["homeassistant/sensor/frisquet_bridge/dhw_temperature/config"]
    dhw_consumption = payloads["homeassistant/sensor/frisquet_bridge/dhw_consumption/config"]
    daily_dhw_consumption = payloads["homeassistant/sensor/frisquet_bridge/daily_dhw_consumption/config"]
    boiler_status = payloads["homeassistant/sensor/frisquet_bridge/boiler_status/config"]
    climate = payloads["homeassistant/climate/frisquet_bridge/zone1_climate/config"]

    assert dhw_temperature["availability_topic"] == "frisquet/frisquet_bridge/availability"
    assert dhw_temperature["payload_available"] == "online"
    assert dhw_temperature["payload_not_available"] == "offline"
    assert dhw_temperature["state_class"] == "measurement"
    assert dhw_temperature["suggested_display_precision"] == 1
    assert dhw_consumption["state_class"] == "total_increasing"
    assert daily_dhw_consumption["name"] == "DHW previous day consumption"
    assert boiler_status["state_topic"] == "frisquet/boiler/status"
    assert climate["availability_topic"] == "frisquet/frisquet_bridge/availability"
    assert "boost" not in climate["preset_modes"]
    assert "homeassistant/sensor/frisquet_bridge/zone1_schedule_raw/config" not in payloads


async def test_publish_state_includes_boiler_status(adapter: tuple[MqttAdapter, BoilerData, FakeOps]) -> None:
    mqtt, data, _ops = adapter
    data.boiler.status = BoilerStatus.STANDBY
    client = FakeMqttClient()

    await mqtt.publish_state(client)  # type: ignore[arg-type]

    assert ("frisquet/boiler/status", "Standby", True) in client.published


async def test_publish_offline_sets_retained_availability(adapter: tuple[MqttAdapter, BoilerData, FakeOps]) -> None:
    mqtt, _data, _ops = adapter
    client = FakeMqttClient()

    await mqtt.publish_offline(client)  # type: ignore[arg-type]

    assert client.published == [("frisquet/frisquet_bridge/availability", "offline", True)]
