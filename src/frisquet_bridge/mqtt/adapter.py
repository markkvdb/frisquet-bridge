"""Optional Home Assistant MQTT adapter (maps dataclasses <-> HA entities)."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiomqtt
import structlog

from frisquet_bridge.boiler_profile import (
    SIMPLE_HVAC_MODES,
    SIMPLE_PRESET_MODES,
    apply_simple_command,
    simple_hvac_mode,
    simple_preset,
    simple_target_temperature,
)
from frisquet_bridge.climate import (
    HVAC_MODES,
    PRESET_BOOST,
    PRESET_MODES,
    apply_zone_intent,
    resolve_zone_intent,
    zone_hvac_action,
    zone_hvac_mode,
    zone_preset,
    zone_target_temperature,
)
from frisquet_bridge.config import BridgeConfig
from frisquet_bridge.connect.ops import BoilerOps
from frisquet_bridge.model import DHW_SELECTABLE_MODES, BoilerData, BoilerStatus, DhwMode
from frisquet_bridge.satellite import VirtualSatellite

log = structlog.get_logger(__name__)

DEVICE_ID = "frisquet_bridge"
CLIMATE_MIN_TEMP = 5
CLIMATE_MAX_TEMP = 30
CLIMATE_TEMP_STEP = 0.5
DHW_PENDING_SECONDS = 20.0

MQTT_PRESET_MODES = tuple(preset for preset in PRESET_MODES if preset != PRESET_BOOST)

# Entities intentionally absent from the current MQTT surface; clear retained
# discovery so Home Assistant removes them after earlier runs.
_RETIRED_ZONE_ENTITIES: tuple[tuple[str, str], ...] = tuple(
    (component, f"zone{zone}_{suffix}")
    for zone in (1, 2, 3)
    for component, suffix in (
        ("select", "mode"),
        ("switch", "boost"),
        ("select", "override"),
        ("sensor", "schedule_raw"),
    )
)
_RETIRED_CENTRAL_FLOW_ENTITIES: tuple[tuple[str, str], ...] = (
    ("sensor", "central_flow_temperature"),
    ("sensor", "central_flow_setpoint_temperature"),
)

_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "dhw_temperature": "Boiler DHW temperature",
        "dhw_instant_temperature": "Boiler DHW instant temperature",
        "cdc_temperature": "Boiler heating body temperature",
        "cdc_safety_temperature": "Boiler heating body safety probe temperature",
        "flue_temperature": "Boiler flue temperature",
        "dhw_power": "Boiler DHW instant power",
        "heating_power": "Boiler heating instant power",
        "pressure": "Boiler pressure",
        "dhw_consumption": "Boiler DHW total consumption",
        "heating_consumption": "Boiler heating total consumption",
        "daily_dhw_consumption": "Boiler DHW previous day consumption",
        "daily_heating_consumption": "Boiler heating previous day consumption",
        "boiler_status": "Boiler status",
        "dhw_mode": "Boiler DHW mode",
        "outside_temperature": "Outside sensor temperature",
        "central_heat_demand": "Central boiler heat demand",
        "central_setpoint": "Central boiler setpoint",
        "central_demand_on_delta": "Central boiler demand delta",
        "central_demand_off_margin": "Central boiler off margin",
        "central_boiler_status": "Central boiler status",
        "central_outside_temperature": "Central boiler outside temperature",
        "zone_heating": "Zone {zone} heating",
        "zone_reported_ambient": "Zone {zone} reported room temperature",
        "zone_flow_temperature": "Zone {zone} flow temperature",
        "zone_flow_setpoint_temperature": "Zone {zone} flow setpoint temperature",
    },
    "fr": {
        "dhw_temperature": "Chaudière température ECS",
        "dhw_instant_temperature": "Chaudière température ECS instantanée",
        "cdc_temperature": "Chaudière température CDC",
        "cdc_safety_temperature": "Chaudière température sonde sécurité CDC",
        "flue_temperature": "Chaudière température fumées",
        "dhw_power": "Chaudière puissance instantanée ECS",
        "heating_power": "Chaudière puissance instantanée chauffage",
        "pressure": "Chaudière pression",
        "dhw_consumption": "Chaudière consommation ECS totale",
        "heating_consumption": "Chaudière consommation chauffage totale",
        "daily_dhw_consumption": "Chaudière consommation ECS jour précédent",
        "daily_heating_consumption": "Chaudière consommation chauffage jour précédent",
        "boiler_status": "Chaudière état",
        "dhw_mode": "Chaudière mode ECS",
        "outside_temperature": "Sonde extérieure température",
        "central_heat_demand": "Chaudière centrale demande chauffage",
        "central_setpoint": "Chaudière centrale consigne",
        "central_demand_on_delta": "Chaudière centrale delta demande",
        "central_demand_off_margin": "Chaudière centrale marge arrêt",
        "central_boiler_status": "Chaudière centrale état chaudière",
        "central_outside_temperature": "Chaudière centrale température extérieure",
        "zone_heating": "Zone {zone} chauffage",
        "zone_reported_ambient": "Zone {zone} température ambiante déclarée",
        "zone_flow_temperature": "Zone {zone} température départ",
        "zone_flow_setpoint_temperature": "Zone {zone} température consigne départ",
    },
}

_DHW_LABELS: dict[str, dict[DhwMode, str]] = {
    "en": {mode: mode.value for mode in DhwMode},
    "fr": {
        DhwMode.MAX: "Max",
        DhwMode.ECO: "Eco",
        DhwMode.ECO_SCHEDULE: "Eco Horaires",
        DhwMode.ECO_PLUS: "Eco+",
        DhwMode.ECO_PLUS_SCHEDULE: "Eco+ Horaires",
        DhwMode.STOP: "Stop",
    },
}

_BOILER_STATUS_LABELS: dict[str, dict[BoilerStatus, str]] = {
    "en": {status: status.value for status in BoilerStatus},
    "fr": {
        BoilerStatus.STANDBY: "Veille",
        BoilerStatus.RUNNING: "En marche",
        BoilerStatus.HEATING_OFF: "Chauffage arrêté",
    },
}


@dataclass(frozen=True, slots=True)
class EntitySpec:
    component: str
    entity_id: str
    label_key: str
    state_suffix: str
    device_class: str | None = None
    unit: str | None = None
    command: bool = False
    options: tuple[str, ...] | None = None
    state_class: str | None = None
    entity_category: str | None = None
    suggested_display_precision: int | None = None


BOILER_SENSOR_ENTITIES = (
    EntitySpec(
        "sensor",
        "dhw_temperature",
        "dhw_temperature",
        "boiler/dhwTemperature",
        "temperature",
        "°C",
        state_class="measurement",
        suggested_display_precision=1,
    ),
    EntitySpec(
        "sensor",
        "dhw_instant_temperature",
        "dhw_instant_temperature",
        "boiler/dhwInstantTemperature",
        "temperature",
        "°C",
        state_class="measurement",
        suggested_display_precision=1,
    ),
    EntitySpec(
        "sensor",
        "cdc_temperature",
        "cdc_temperature",
        "boiler/cdcTemperature",
        "temperature",
        "°C",
        state_class="measurement",
        suggested_display_precision=1,
    ),
    EntitySpec(
        "sensor",
        "cdc_safety_temperature",
        "cdc_safety_temperature",
        "boiler/cdcSafetyTemperature",
        "temperature",
        "°C",
        state_class="measurement",
        suggested_display_precision=1,
    ),
    EntitySpec(
        "sensor",
        "flue_temperature",
        "flue_temperature",
        "boiler/flueTemperature",
        "temperature",
        "°C",
        state_class="measurement",
        suggested_display_precision=1,
    ),
    EntitySpec(
        "sensor",
        "dhw_power",
        "dhw_power",
        "boiler/dhwInstantPower",
        "power",
        "kW",
        state_class="measurement",
        suggested_display_precision=1,
    ),
    EntitySpec(
        "sensor",
        "heating_power",
        "heating_power",
        "boiler/heatingInstantPower",
        "power",
        "kW",
        state_class="measurement",
        suggested_display_precision=1,
    ),
    EntitySpec(
        "sensor", "pressure", "pressure", "boiler/pressure", "pressure", "bar", state_class="measurement", suggested_display_precision=1
    ),
    EntitySpec(
        "sensor", "dhw_consumption", "dhw_consumption", "boiler/dhwConsumption", "energy", "kWh", state_class="total_increasing"
    ),
    EntitySpec(
        "sensor",
        "heating_consumption",
        "heating_consumption",
        "boiler/heatingConsumption",
        "energy",
        "kWh",
        state_class="total_increasing",
    ),
    EntitySpec(
        "sensor",
        "daily_dhw_consumption",
        "daily_dhw_consumption",
        "boiler/dailyDhwConsumption",
        "energy",
        "kWh",
        state_class="measurement",
    ),
    EntitySpec(
        "sensor",
        "daily_heating_consumption",
        "daily_heating_consumption",
        "boiler/dailyHeatingConsumption",
        "energy",
        "kWh",
        state_class="measurement",
    ),
    EntitySpec("sensor", "boiler_status", "boiler_status", "boiler/status"),
)

DHW_MODE_ENTITY = EntitySpec(
    "select",
    "dhw_mode",
    "dhw_mode",
    "boiler/dhwMode",
    command=True,
    options=tuple(m.value for m in DHW_SELECTABLE_MODES),
)

DHW_MODE_SENSOR_ENTITY = EntitySpec(
    "sensor",
    "dhw_mode",
    "dhw_mode",
    "boiler/dhwMode",
)

SONDE_ENTITIES = (
    EntitySpec(
        "number",
        "outside_temperature",
        "outside_temperature",
        "outsideSensor/outsideTemperature",
        "temperature",
        "°C",
        command=True,
    ),
)


class MqttAdapter:
    """Thin HA adapter over BoilerData; core library never imports this at runtime unless enabled."""

    def __init__(
        self,
        cfg: BridgeConfig,
        data: BoilerData,
        ops: BoilerOps | None,
        *,
        sonde_ops: BoilerOps | None = None,
        virtual_satellites: dict[int, VirtualSatellite] | None = None,
        on_state_change: Callable[[], Awaitable[None]] | None = None,
        on_persist_state: Callable[[], None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._data = data
        self._ops = ops
        self._sonde_ops = sonde_ops
        self._virtual = virtual_satellites or {}
        self._on_change = on_state_change
        self._on_persist_state = on_persist_state
        self._base = cfg.mqtt.base_topic.rstrip("/")
        self._published: set[str] = set()
        self._retired_cleared = False
        self._pending_dhw_mode: DhwMode | None = None
        self._pending_dhw_until = 0.0
        self._last_published_dhw_mode: DhwMode | None = None

    def _topic(self, suffix: str) -> str:
        return f"{self._base}/{suffix}"

    def availability_topic(self) -> str:
        return self._topic(f"{DEVICE_ID}/availability")

    def _discovery_topic(self, component: str, entity_id: str) -> str:
        return f"homeassistant/{component}/{DEVICE_ID}/{entity_id}/config"

    def _label(self, key: str, **placeholders: object) -> str:
        template = _LABELS.get(self._cfg.mqtt.language, _LABELS["en"]).get(key, _LABELS["en"].get(key, key))
        return template.format(**placeholders)

    def _dhw_label(self, mode: DhwMode) -> str:
        return _DHW_LABELS.get(self._cfg.mqtt.language, _DHW_LABELS["en"])[mode]

    def _boiler_status_label(self, status: BoilerStatus | None) -> str | None:
        if status is None:
            return None
        return _BOILER_STATUS_LABELS.get(self._cfg.mqtt.language, _BOILER_STATUS_LABELS["en"])[status]

    def _device_block(self) -> dict[str, Any]:
        return {
            "identifiers": [DEVICE_ID],
            "name": "Frisquet Bridge",
            "manufacturer": "Frisquet",
            "model": "Eco Radio Visio",
        }

    def _origin_block(self) -> dict[str, Any]:
        return {"name": "frisquet-bridge"}

    def _base_discovery_payload(self, spec: EntitySpec) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self._label(spec.label_key),
            "unique_id": f"{DEVICE_ID}_{spec.entity_id}",
            "state_topic": self._topic(spec.state_suffix),
            "availability_topic": self.availability_topic(),
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": self._device_block(),
            "origin": self._origin_block(),
        }
        if spec.device_class:
            payload["device_class"] = spec.device_class
        if spec.unit:
            payload["unit_of_measurement"] = spec.unit
        if spec.command:
            payload["command_topic"] = self._topic(f"{spec.state_suffix}/set")
        if spec.options:
            if spec.entity_id == "dhw_mode":
                payload["options"] = [self._dhw_label(mode) for mode in DHW_SELECTABLE_MODES]
            else:
                payload["options"] = list(spec.options)
        if spec.state_class:
            payload["state_class"] = spec.state_class
        if spec.entity_category:
            payload["entity_category"] = spec.entity_category
        if spec.suggested_display_precision is not None:
            payload["suggested_display_precision"] = spec.suggested_display_precision
        if spec.component == "number":
            payload["mode"] = "box"
            payload["step"] = 0.1
        return payload

    def _add_availability(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload["availability_topic"] = self.availability_topic()
        payload["payload_available"] = "online"
        payload["payload_not_available"] = "offline"
        payload["origin"] = self._origin_block()
        return payload

    def _zone_config(self, zone: int) -> Any:
        return self._cfg.zone(zone)

    def _is_simple_zone(self, zone: int) -> bool:
        zone_cfg = self._zone_config(zone)
        return zone_cfg is not None and zone_cfg.is_simple_satellite

    def _is_central_zone(self, zone: int) -> bool:
        zone_cfg = self._zone_config(zone)
        return zone_cfg is not None and zone_cfg.is_central_boiler

    def _is_read_only_satellite_zone(self, zone: int) -> bool:
        zone_cfg = self._zone_config(zone)
        return (
            zone_cfg is not None
            and zone_cfg.is_read_only_satellite
            and not self._cfg.connect_physical_zone_control_enabled
        )

    def _boiler_entities_enabled(self) -> bool:
        return self._cfg.boiler_entities_enabled

    def _boiler_commands_enabled(self) -> bool:
        return self._ops is not None and self._cfg.connect_writes_enabled

    async def _publish_discovery_payload(
        self,
        client: aiomqtt.Client,
        component: str,
        entity_id: str,
        payload: dict[str, Any],
    ) -> bool:
        key = f"{component}.{entity_id}"
        if key in self._published:
            return False
        await client.publish(self._discovery_topic(component, entity_id), json.dumps(payload), retain=True)
        self._published.add(key)
        return True

    async def _clear_discovery(self, client: aiomqtt.Client, component: str, entity_id: str) -> None:
        await client.publish(self._discovery_topic(component, entity_id), "", retain=True)

    async def _publish_central_discovery(self, client: aiomqtt.Client) -> int:
        count = 0
        device = self._device_block()
        switch_payload = {
            "name": self._label("central_heat_demand"),
            "unique_id": f"{DEVICE_ID}_central_heat_demand",
            "state_topic": self._topic("central/demand"),
            "command_topic": self._topic("central/demand/set"),
            "payload_on": "ON",
            "payload_off": "OFF",
            "device": device,
        }
        self._add_availability(switch_payload)
        if await self._publish_discovery_payload(client, "switch", "central_heat_demand", switch_payload):
            count += 1

        number_specs = (
            ("central_setpoint", "central_setpoint", "central/setpoint", 5, 30, 0.5),
            ("central_demand_on_delta", "central_demand_on_delta", "central/demandOnDelta", 0, 15, 0.1),
            ("central_demand_off_margin", "central_demand_off_margin", "central/demandOffMargin", 0, 15, 0.1),
        )
        for entity_id, label_key, suffix, min_value, max_value, step in number_specs:
            payload = {
                "name": self._label(label_key),
                "unique_id": f"{DEVICE_ID}_{entity_id}",
                "state_topic": self._topic(suffix),
                "command_topic": self._topic(f"{suffix}/set"),
                "device_class": "temperature",
                "unit_of_measurement": "°C",
                "min": min_value,
                "max": max_value,
                "mode": "box",
                "step": step,
                "entity_category": "config",
                "device": device,
            }
            self._add_availability(payload)
            if await self._publish_discovery_payload(client, "number", entity_id, payload):
                count += 1

        sensor_specs = (
            ("central_boiler_status", "central_boiler_status", "central/boilerStatus", None, None),
            ("central_outside_temperature", "central_outside_temperature", "central/outsideTemperature", "temperature", "°C"),
        )
        for entity_id, label_key, suffix, device_class, unit in sensor_specs:
            payload = {
                "name": self._label(label_key),
                "unique_id": f"{DEVICE_ID}_{entity_id}",
                "state_topic": self._topic(suffix),
                "device": device,
            }
            self._add_availability(payload)
            if device_class:
                payload["device_class"] = device_class
                payload["state_class"] = "measurement"
                payload["suggested_display_precision"] = 1
            if unit:
                payload["unit_of_measurement"] = unit
            if await self._publish_discovery_payload(client, "sensor", entity_id, payload):
                count += 1
        return count

    async def _publish_zone_flow_discovery(self, client: aiomqtt.Client, zone: int) -> int:
        count = 0
        for entity_id, label_key, suffix in (
            (f"zone{zone}_flow_temperature", "zone_flow_temperature", f"z{zone}/flowTemperature"),
            (f"zone{zone}_flow_setpoint_temperature", "zone_flow_setpoint_temperature", f"z{zone}/flowSetpointTemperature"),
        ):
            payload = {
                "name": self._label(label_key, zone=zone),
                "unique_id": f"{DEVICE_ID}_{entity_id}",
                "state_topic": self._topic(suffix),
                "device_class": "temperature",
                "state_class": "measurement",
                "unit_of_measurement": "°C",
                "suggested_display_precision": 1,
                "device": self._device_block(),
            }
            self._add_availability(payload)
            if await self._publish_discovery_payload(client, "sensor", entity_id, payload):
                count += 1
        return count

    async def publish_discovery(self, client: aiomqtt.Client) -> None:
        specs = list(BOILER_SENSOR_ENTITIES) if self._boiler_entities_enabled() else []
        if self._boiler_entities_enabled():
            specs.append(DHW_MODE_ENTITY if self._boiler_commands_enabled() else DHW_MODE_SENSOR_ENTITY)
        if self._cfg.sonde is not None and self._cfg.sonde.enabled:
            specs.extend(SONDE_ENTITIES)
        published_count = 0
        for spec in specs:
            key = f"{spec.component}.{spec.entity_id}"
            if key in self._published:
                continue
            payload = self._base_discovery_payload(spec)
            await client.publish(self._discovery_topic(spec.component, spec.entity_id), json.dumps(payload), retain=True)
            self._published.add(key)
            published_count += 1
        if not self._boiler_entities_enabled():
            for spec in BOILER_SENSOR_ENTITIES:
                await self._clear_discovery(client, spec.component, spec.entity_id)
        if not self._boiler_entities_enabled():
            await self._clear_discovery(client, DHW_MODE_ENTITY.component, DHW_MODE_ENTITY.entity_id)
            await self._clear_discovery(client, DHW_MODE_SENSOR_ENTITY.component, DHW_MODE_SENSOR_ENTITY.entity_id)
        elif self._boiler_commands_enabled():
            await self._clear_discovery(client, DHW_MODE_SENSOR_ENTITY.component, DHW_MODE_SENSOR_ENTITY.entity_id)
        else:
            await self._clear_discovery(client, DHW_MODE_ENTITY.component, DHW_MODE_ENTITY.entity_id)

        for zone in (1, 2, 3):
            if not self._cfg.zone_enabled(zone):
                continue
            published_count += await self._publish_zone_flow_discovery(client, zone)
            if self._is_central_zone(zone):
                published_count += await self._publish_central_discovery(client)
                await self._clear_discovery(client, "climate", f"zone{zone}_climate")
                await self._clear_discovery(client, "number", f"zone{zone}_reported_ambient")
                continue
            entity_id = f"zone{zone}_climate"
            key = f"climate.{entity_id}"
            if key not in self._published:
                prefix = f"z{zone}"
                modes = SIMPLE_HVAC_MODES if self._is_simple_zone(zone) else HVAC_MODES
                preset_modes = SIMPLE_PRESET_MODES if self._is_simple_zone(zone) else MQTT_PRESET_MODES
                climate_payload: dict[str, Any] = {
                    "name": self._label("zone_heating", zone=zone),
                    "unique_id": f"{DEVICE_ID}_{entity_id}",
                    "modes": list(modes),
                    "mode_state_topic": self._topic(f"{prefix}/mode"),
                    "preset_modes": list(preset_modes),
                    "preset_mode_state_topic": self._topic(f"{prefix}/preset"),
                    "action_topic": self._topic(f"{prefix}/action"),
                    "current_temperature_topic": self._topic(f"{prefix}/currentTemperature"),
                    "temperature_state_topic": self._topic(f"{prefix}/targetTemperature"),
                    "min_temp": CLIMATE_MIN_TEMP,
                    "max_temp": CLIMATE_MAX_TEMP,
                    "temp_step": CLIMATE_TEMP_STEP,
                    "temperature_unit": "C",
                    "precision": 0.1,
                    "device": self._device_block(),
                }
                if not self._is_read_only_satellite_zone(zone):
                    climate_payload.update(
                        {
                            "mode_command_topic": self._topic(f"{prefix}/mode/set"),
                            "preset_mode_command_topic": self._topic(f"{prefix}/preset/set"),
                            "temperature_command_topic": self._topic(f"{prefix}/targetTemperature/set"),
                        }
                    )
                self._add_availability(climate_payload)
                await client.publish(self._discovery_topic("climate", entity_id), json.dumps(climate_payload), retain=True)
                self._published.add(key)
                published_count += 1

            # Virtual-satellite zones expose the room temperature as an input so
            # Home Assistant (e.g. a real room sensor) can feed what we report
            # to the boiler on that zone's behalf.
            if zone in self._virtual and not self._is_central_zone(zone):
                ambient_id = f"zone{zone}_reported_ambient"
                ambient_key = f"number.{ambient_id}"
                if ambient_key not in self._published:
                    ambient_payload = {
                        "name": self._label("zone_reported_ambient", zone=zone),
                        "unique_id": f"{DEVICE_ID}_{ambient_id}",
                        "state_topic": self._topic(f"z{zone}/reportedAmbient"),
                        "command_topic": self._topic(f"z{zone}/reportedAmbient/set"),
                        "device_class": "temperature",
                        "unit_of_measurement": "°C",
                        "min": CLIMATE_MIN_TEMP,
                        "max": 35,
                        "mode": "box",
                        "step": 0.1,
                        "device": self._device_block(),
                    }
                    self._add_availability(ambient_payload)
                    await client.publish(self._discovery_topic("number", ambient_id), json.dumps(ambient_payload), retain=True)
                    self._published.add(ambient_key)
                    published_count += 1

        if not self._retired_cleared:
            for component, entity_id in _RETIRED_ZONE_ENTITIES:
                await client.publish(self._discovery_topic(component, entity_id), "", retain=True)
            for component, entity_id in _RETIRED_CENTRAL_FLOW_ENTITIES:
                await client.publish(self._discovery_topic(component, entity_id), "", retain=True)
            self._retired_cleared = True

        avail = self.availability_topic()
        await client.publish(avail, "online", retain=True)
        log.info("mqtt_discovery_published", entity_count=published_count, availability_topic=avail)

    async def publish_offline(self, client: aiomqtt.Client) -> None:
        await client.publish(self.availability_topic(), "offline", retain=True)

    async def publish_state(self, client: aiomqtt.Client) -> None:
        b = self._data.boiler
        mapping: dict[str, str | None] = {}
        if self._boiler_entities_enabled():
            mapping.update(
                {
                    "boiler/dhwTemperature": _fmt(b.dhw_temperature),
                    "boiler/dhwInstantTemperature": _fmt(b.dhw_instant_temperature),
                    "boiler/cdcTemperature": _fmt(b.cdc_temperature),
                    "boiler/cdcSafetyTemperature": _fmt(b.cdc_safety_temperature),
                    "boiler/flueTemperature": _fmt(b.flue_temperature),
                    "boiler/dhwInstantPower": _fmt(b.dhw_power),
                    "boiler/heatingInstantPower": _fmt(b.heating_power),
                    "boiler/pressure": _fmt(b.pressure),
                    "boiler/dhwConsumption": _fmt_int(b.dhw_consumption),
                    "boiler/heatingConsumption": _fmt_int(b.heating_consumption),
                    "boiler/dailyDhwConsumption": _fmt_int(b.daily_dhw_consumption),
                    "boiler/dailyHeatingConsumption": _fmt_int(b.daily_heating_consumption),
                    "boiler/status": self._boiler_status_label(b.status),
                }
            )
        if self._boiler_entities_enabled():
            dhw_mode = self._visible_dhw_mode()
            mapping["boiler/dhwMode"] = self._dhw_label(dhw_mode) if dhw_mode else None
        if self._cfg.sonde is not None and self._cfg.sonde.enabled:
            mapping["outsideSensor/outsideTemperature"] = _fmt(self._data.sonde.outside_temperature)
        for zone, state in self._data.zones.items():
            if not self._cfg.zone_enabled(zone):
                continue
            prefix = f"z{zone}"
            mapping[f"{prefix}/flowTemperature"] = _fmt(state.flow_temperature)
            mapping[f"{prefix}/flowSetpointTemperature"] = _fmt(state.flow_setpoint_temperature)
            if self._is_central_zone(zone):
                mapping["central/demand"] = _fmt_bool(state.central_demand)
                mapping["central/setpoint"] = _fmt(state.central_setpoint)
                mapping["central/demandOnDelta"] = _fmt(state.central_demand_on_delta)
                mapping["central/demandOffMargin"] = _fmt(state.central_demand_off_margin)
                mapping["central/boilerStatus"] = self._boiler_status_label(b.status)
                mapping["central/outsideTemperature"] = _fmt(b.outside_temperature)
                continue
            mapping[f"{prefix}/mode"] = simple_hvac_mode(state) if self._is_simple_zone(zone) else zone_hvac_mode(state)
            mapping[f"{prefix}/preset"] = simple_preset(state) if self._is_simple_zone(zone) else zone_preset(state)
            mapping[f"{prefix}/action"] = zone_hvac_action(state, b)
            target = simple_target_temperature(state) if self._is_simple_zone(zone) else zone_target_temperature(state)
            mapping[f"{prefix}/targetTemperature"] = _fmt(target)
            if zone in self._virtual:
                # For a virtual satellite the boiler has no room sensor of its
                # own: the "current temperature" is the ambient we report, fed
                # from Home Assistant.
                current = state.reported_ambient if state.reported_ambient is not None else state.ambient_temperature
                mapping[f"{prefix}/currentTemperature"] = _fmt(current)
                mapping[f"{prefix}/reportedAmbient"] = _fmt(state.reported_ambient)
            else:
                mapping[f"{prefix}/currentTemperature"] = _fmt(state.ambient_temperature)
        published_count = 0
        for suffix, value in mapping.items():
            if value is not None:
                await client.publish(self._topic(suffix), value, retain=True)
                published_count += 1
        if self._boiler_entities_enabled():
            dhw_mode = self._visible_dhw_mode()
            if dhw_mode != self._last_published_dhw_mode:
                self._last_published_dhw_mode = dhw_mode
                log.info(
                    "mqtt_dhw_state_published",
                    mode=self._dhw_label(dhw_mode) if dhw_mode is not None else None,
                    pending=self._pending_dhw_mode is not None and time.monotonic() < self._pending_dhw_until,
                )
        log.debug("mqtt_state_published", topic_count=published_count)

    async def handle_command(self, topic: str, payload: str) -> None:
        payload = payload.strip()
        log.info("mqtt_command_received", topic=topic, payload=payload)
        if topic.endswith("boiler/dhwMode/set"):
            if not self._boiler_commands_enabled():
                raise RuntimeError("DHW mode writes require a paired [frisquet.connect] identity")
            mode = DhwMode.parse(payload)
            if self._pending_dhw_mode == mode and time.monotonic() < self._pending_dhw_until:
                log.debug("mqtt_command_skipped", topic=topic, reason="dhw_mode_already_pending", mode=mode.value)
                return
            assert self._ops is not None
            self._pending_dhw_mode = mode
            self._pending_dhw_until = time.monotonic() + DHW_PENDING_SECONDS
            if self._on_change:
                await self._on_change()

            try:
                await self._ops.write_dhw_mode(self._data, mode)
            except Exception:
                self._pending_dhw_mode = None
                self._pending_dhw_until = 0.0
                if self._on_change:
                    await self._on_change()
                raise
            if self._on_change:
                await self._on_change()
        elif topic.endswith("outsideSensor/outsideTemperature/set") and self._sonde_ops is not None:
            await self._sonde_ops.write_outside_temperature(self._data, float(payload))
            if self._on_change:
                await self._on_change()
        else:
            central_command = self._central_command(topic)
            if central_command is not None:
                await self._handle_central_command(central_command, payload)
                if self._on_change:
                    await self._on_change()
                return
            zone_command = self._zone_command(topic)
            if zone_command is None:
                log.warning("mqtt_command_ignored", topic=topic)
                return
            zone, command = zone_command
            if self._is_central_zone(zone):
                log.warning("mqtt_command_ignored", topic=topic)
                return
            if zone in self._virtual and not self._is_central_zone(zone):
                await self._handle_virtual_zone_command(zone, command, payload)
            elif self._is_read_only_satellite_zone(zone):
                log.warning("mqtt_command_ignored", topic=topic, reason="read_only_satellite_zone")
                return
            elif self._ops is not None:
                await self._handle_connect_zone_command(zone, command, payload)
            else:
                log.warning("mqtt_command_ignored", topic=topic)
                return
            if self._on_change:
                await self._on_change()

    async def handle_message(self, client: aiomqtt.Client, topic: str, payload: str) -> None:
        body = payload.strip()
        if topic == "homeassistant/status":
            if body == "online":
                self._published.clear()
                self._retired_cleared = False
                await self.publish_discovery(client)
                await self.publish_state(client)
                log.info("mqtt_homeassistant_birth_handled")
            return
        await self.handle_command(topic, body)

    async def _handle_central_command(self, command: str, payload: str) -> None:
        if not self._is_central_zone(1):
            log.warning("mqtt_command_ignored", command=command)
            return
        zone = self._data.zones[1]
        if command == "demand":
            zone.central_demand = _parse_bool(payload)
        elif command == "setpoint":
            zone.central_setpoint = round(float(payload), 1)
        elif command == "demandOnDelta":
            zone.central_demand_on_delta = round(float(payload), 1)
        elif command == "demandOffMargin":
            zone.central_demand_off_margin = round(float(payload), 1)
        else:
            log.warning("mqtt_command_ignored", command=command)
            return
        self._persist_state()
        satellite = self._virtual.get(1)
        if satellite is not None:
            await satellite.send_now()

    async def _handle_connect_zone_command(self, zone: int, command: str, payload: str) -> None:
        assert self._ops is not None
        if command == "mode":
            await self._ops.apply_zone_climate(zone, self._data, hvac_mode=payload)
        elif command == "preset":
            await self._ops.apply_zone_climate(zone, self._data, preset=payload)
        elif command == "targetTemperature":
            await self._ops.apply_zone_climate(zone, self._data, target_temperature=float(payload))
        else:
            log.warning("mqtt_command_ignored", zone=zone, command=command)

    async def _handle_virtual_zone_command(self, zone: int, command: str, payload: str) -> None:
        satellite = self._virtual[zone]
        zs = self._data.zones[zone]
        if command == "reportedAmbient":
            zs.reported_ambient = float(payload)
        elif command == "mode":
            if self._is_simple_zone(zone):
                apply_simple_command(zs, hvac_mode=payload)
            else:
                apply_zone_intent(zs, resolve_zone_intent(zs, hvac_mode=payload))
        elif command == "preset":
            if self._is_simple_zone(zone):
                apply_simple_command(zs, preset=payload)
            else:
                apply_zone_intent(zs, resolve_zone_intent(zs, preset=payload))
        elif command == "targetTemperature":
            if self._is_simple_zone(zone):
                apply_simple_command(zs, target_temperature=float(payload))
            else:
                apply_zone_intent(zs, resolve_zone_intent(zs, target_temperature=float(payload)))
        else:
            log.warning("mqtt_command_ignored", zone=zone, command=command)
            return
        self._persist_state()
        await satellite.send_now()

    def _persist_state(self) -> None:
        if self._on_persist_state is not None:
            self._on_persist_state()

    def _visible_dhw_mode(self) -> DhwMode | None:
        if self._pending_dhw_mode is None:
            return self._data.boiler.dhw_mode
        if time.monotonic() < self._pending_dhw_until:
            return self._pending_dhw_mode
        self._pending_dhw_mode = None
        self._pending_dhw_until = 0.0
        return self._data.boiler.dhw_mode

    def _central_command(self, topic: str) -> str | None:
        prefix = f"{self._base}/"
        if not topic.startswith(prefix):
            return None
        parts = topic[len(prefix) :].split("/")
        if len(parts) != 3 or parts[0] != "central" or parts[2] != "set":
            return None
        if parts[1] not in {"demand", "setpoint", "demandOnDelta", "demandOffMargin"}:
            return None
        return parts[1]

    def _zone_command(self, topic: str) -> tuple[int, str] | None:
        prefix = f"{self._base}/"
        if not topic.startswith(prefix):
            return None
        parts = topic[len(prefix) :].split("/")
        if len(parts) != 3 or parts[2] != "set":
            return None
        zone_part = parts[0]
        if len(zone_part) != 2 or zone_part[0] != "z" or not zone_part[1].isdigit():
            return None
        zone = int(zone_part[1])
        if zone not in (1, 2, 3) or not self._cfg.zone_enabled(zone):
            return None
        if parts[1] not in {"mode", "preset", "targetTemperature", "reportedAmbient"}:
            return None
        return zone, parts[1]

    async def run(self, client: aiomqtt.Client) -> None:
        await self.publish_discovery(client)
        await self.publish_state(client)
        cmd_filter = f"{self._base}/+/+/set"
        await client.subscribe(cmd_filter)
        await client.subscribe("homeassistant/status")
        log.info("mqtt_subscribed", topic_filter=cmd_filter)
        log.info("mqtt_subscribed", topic_filter="homeassistant/status")
        async for message in client.messages:
            topic = str(message.topic)
            body = message.payload.decode("utf-8", errors="replace")
            try:
                await self.handle_message(client, topic, body)
            except Exception as exc:
                log.exception("mqtt_command_failed", topic=topic, error=str(exc))


def _fmt(value: float | None) -> str | None:
    return None if value is None else f"{value:.1f}"


def _fmt_int(value: int | None) -> str | None:
    return None if value is None else str(value)


def _fmt_bool(value: bool | None) -> str | None:
    if value is None:
        return None
    return "ON" if value else "OFF"


def _parse_bool(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "on", "yes", "heat"}:
        return True
    if normalized in {"0", "false", "off", "no", "idle"}:
        return False
    raise ValueError(f"expected boolean payload, got {value!r}")
