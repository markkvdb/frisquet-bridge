'Optional Home Assistant MQTT adapter (maps dataclasses <-> HA entities).'

from __future__ import annotations

import asyncio, json, re, time
from collections.abc import Callable
from typing import Any

import aiomqtt, structlog

from frisquet_bridge.boiler_profile import (SIMPLE_HVAC_MODES, SIMPLE_PRESET_MODES, apply_simple_command, flow_temp_outdoor, flow_temp_range,
    simple_hvac_mode, simple_preset, simple_target_temperature)
from frisquet_bridge.climate import (HVAC_MODES, PRESET_BOOST, PRESET_MODES, apply_zone_intent, resolve_zone_intent, zone_hvac_action, zone_hvac_mode,
    zone_preset, zone_target_temperature)
from frisquet_bridge.config import ZONE1_ONLY_MODES, BridgeConfig
from frisquet_bridge.connect.ops import BoilerOps
from frisquet_bridge.model import (DHW_SELECTABLE_MODES, SONDE_MAX_TEMPERATURE, SONDE_MIN_TEMPERATURE, BoilerData, BoilerStatus, DhwMode,
    daily_consumption_reset)
from frisquet_bridge.satellite import VirtualSatellite
from frisquet_bridge.types import TemperatureWriter, UpdateNotify

log = structlog.get_logger(__name__)

DEVICE_ID = "frisquet_bridge"
CLIMATE_MIN_TEMP = 5
CLIMATE_MAX_TEMP = 30
CLIMATE_TEMP_STEP = 0.5
DHW_PENDING_SECONDS = 20.0
FALLBACK_SECONDS = 1800.0 # How long HA's MQTT status must stay offline before flow_temp falls back to its fallback flow

MQTT_PRESET_MODES = tuple(preset for preset in PRESET_MODES if preset != PRESET_BOOST)

_Config = dict[str, Any] # one discovery component, or the device block
DEVICE_TOPIC = f"homeassistant/device/{DEVICE_ID}/config"
# Discovery topics from before the bridge used device discovery, one per entity.
_LEGACY_DISCOVERY = re.compile(rf"homeassistant/(\w+)/{DEVICE_ID}/(\w+)/config")
_PROFILE_COMMANDS = {"central": {"demand", "setpoint", "demandOnDelta", "demandOffMargin"}, "flow": {"demand", "target"}}

_LABELS = {  # chkstyle: ignore-node
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
        "flow_demand": "Flow temperature heating",
        "flow_target": "Flow temperature target",
        "flow_outside_temperature": "Virtual outside temperature",
        "zone_heating": "Zone {zone} heating",
        "zone_reported_ambient": "Zone {zone} reported room temperature",
        "zone_flow_temperature": "Zone {zone} flow temperature",
        "zone_flow_setpoint_temperature": "Zone {zone} flow setpoint temperature",
        "rf_operation": "RF {operation}",
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
        "flow_demand": "Chauffage par température de départ",
        "flow_target": "Consigne température de départ",
        "flow_outside_temperature": "Température extérieure virtuelle",
        "zone_heating": "Zone {zone} chauffage",
        "zone_reported_ambient": "Zone {zone} température ambiante déclarée",
        "zone_flow_temperature": "Zone {zone} température départ",
        "zone_flow_setpoint_temperature": "Zone {zone} température consigne départ",
        "rf_operation": "RF {operation}",
    }}

_DHW_LABELS = {  # chkstyle: ignore-node
    "en": {mode: mode.value for mode in DhwMode},
    "fr": {
        DhwMode.MAX: "Max",
        DhwMode.ECO: "Eco",
        DhwMode.ECO_SCHEDULE: "Eco Horaires",
        DhwMode.ECO_PLUS: "Eco+",
        DhwMode.ECO_PLUS_SCHEDULE: "Eco+ Horaires",
        DhwMode.STOP: "Stop",
    }}

_BOILER_STATUS_LABELS = {'en': {status: status.value for status in BoilerStatus},
    'fr': {BoilerStatus.STANDBY: 'Veille', BoilerStatus.RUNNING: 'En marche', BoilerStatus.HEATING_OFF: 'Chauffage arrêté'}}


# Boiler sensors: entity id, state topic under boiler/, device class, unit and kind (see _SENSOR_KINDS).
_BOILER_SENSORS = (  # chkstyle: ignore-node
    ('dhw_temperature', 'dhwTemperature', 'temperature', '°C', 'measurement'),
    ('dhw_instant_temperature', 'dhwInstantTemperature', 'temperature', '°C', 'measurement'),
    ('cdc_temperature', 'cdcTemperature', 'temperature', '°C', 'measurement'),
    ('cdc_safety_temperature', 'cdcSafetyTemperature', 'temperature', '°C', 'measurement'),
    ('flue_temperature', 'flueTemperature', 'temperature', '°C', 'measurement'),
    ('dhw_power', 'dhwInstantPower', 'power', 'kW', 'measurement'),
    ('heating_power', 'heatingInstantPower', 'power', 'kW', 'measurement'),
    ('pressure', 'pressure', 'pressure', 'bar', 'measurement'),
    ('dhw_consumption', 'dhwConsumption', 'energy', 'kWh', 'diagnostic'),
    ('heating_consumption', 'heatingConsumption', 'energy', 'kWh', 'diagnostic'),
    ('daily_dhw_consumption', 'dailyDhwConsumption', 'energy', 'kWh', 'daily'),
    ('daily_heating_consumption', 'dailyHeatingConsumption', 'energy', 'kWh', 'daily'),
    ('boiler_status', 'status', None, None, None))
# The discovery options each kind of boiler sensor adds.
_SENSOR_KINDS = dict(measurement=dict(state_class='measurement'),
    # The previous-day counters are published with the day they cover (see `_publish_state`).
    daily=dict(state_class='total', value_template='{{ value_json.value }}', last_reset_value_template='{{ value_json.last_reset }}'),
    # The lifetime totals have not changed since at least 22 October 2026 while the daily counters moved, so they stay out of HA's statistics.
    diagnostic=dict(entity_category='diagnostic'))


class MqttAdapter:
    'Thin HA adapter over BoilerData; core library never imports this at runtime unless enabled.'

    def __init__(
        self,
        cfg: BridgeConfig,
        data: BoilerData,
        ops: BoilerOps | None,
        *,
        sonde_ops: BoilerOps | None = None,
        outside_temperature_writer: TemperatureWriter | None = None, # alternative outside-sensor writer
        virtual_satellites: dict[int, VirtualSatellite] | None = None,
        on_state_change: UpdateNotify | None = None,
        on_persist_state: Callable[[], None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._data = data
        self._ops = ops
        self._sonde_ops = sonde_ops
        self._outside_temperature_writer = outside_temperature_writer
        self._virtual = virtual_satellites or {}
        self._on_change = on_state_change
        self._on_persist_state = on_persist_state
        self._base = cfg.mqtt.base_topic.rstrip("/")
        self._discovered = {} # device-discovery key -> platform of each entity HA currently has
        self._last_discovery = None
        self._published_state = {} # state suffix -> last value published
        self._pending_dhw_mode = None
        self._pending_dhw_until = 0.0
        self._last_published_dhw_mode = None
        self._publish_lock = asyncio.Lock()
        self._ha_offline_since = None # monotonic time at which HA's MQTT status turned offline
        self._falling_back = False

    def _topic(self, suffix: str) -> str: return f"{self._base}/{suffix}"

    def availability_topic(self) -> str: return self._topic(f"{DEVICE_ID}/availability")

    def _label(self, key: str, **placeholders: object) -> str:
        template = _LABELS.get(self._cfg.mqtt.language, _LABELS["en"]).get(key, _LABELS["en"].get(key, key))
        return template.format(**placeholders)

    def _dhw_label(self, mode: DhwMode) -> str: return _DHW_LABELS.get(self._cfg.mqtt.language, _DHW_LABELS["en"])[mode]

    def _boiler_status_label(self, status: BoilerStatus | None) -> str | None:
        if status is None: return None
        return _BOILER_STATUS_LABELS.get(self._cfg.mqtt.language, _BOILER_STATUS_LABELS["en"])[status]

    def _device_block(self) -> _Config:
        return dict(identifiers=[DEVICE_ID], name='Frisquet Bridge', manufacturer='Frisquet', model='Eco Radio Visio')

    def _origin_block(self) -> _Config: return {"name": "frisquet-bridge"}

    def _zone_config(self, zone: int) -> Any: return self._cfg.zone(zone)

    def _is_simple_zone(self, zone: int) -> bool:
        zone_cfg = self._zone_config(zone)
        return zone_cfg is not None and zone_cfg.is_simple_satellite

    def _is_central_zone(self, zone: int) -> bool:
        zone_cfg = self._zone_config(zone)
        return zone_cfg is not None and zone_cfg.is_central_boiler

    def _is_flow_zone(self, zone: int) -> bool: return zone == 1 and self._cfg.flow_curve is not None

    def _is_zone1_profile(self, zone: int) -> bool:
        zone_cfg = self._zone_config(zone)
        return zone_cfg is not None and zone_cfg.mode in ZONE1_ONLY_MODES

    def _sonde_exposed(self) -> bool:
        'Whether HA writes the outside sensor, which the bridge does itself in flow_temp mode'
        return self._cfg.sonde is not None and self._cfg.sonde.enabled and self._cfg.flow_curve is None

    def _is_read_only_satellite_zone(self, zone: int) -> bool:
        zone_cfg = self._zone_config(zone)
        return (
            zone_cfg is not None and zone_cfg.is_read_only_satellite and (not self._cfg.connect_physical_zone_control_enabled))

    def _boiler_entities_enabled(self) -> bool: return self._cfg.boiler_entities_enabled

    def _boiler_commands_enabled(self) -> bool: return self._ops is not None and self._cfg.connect_writes_enabled

    def _component(self, platform: str, entity_id: str, name: str | None = None, **config: Any) -> tuple[str, _Config]:
        "A device-discovery component, named by its label unless `name` is given. Keys include the platform, so a platform change removes the old entity"
        return f"{platform}_{entity_id}", dict(p=platform, name=name or self._label(entity_id), unique_id=f"{DEVICE_ID}_{entity_id}",
            default_entity_id=f"{platform}.{DEVICE_ID}_{entity_id}", **{key: value for key, value in config.items() if value is not None})

    def _temperature_sensor(self, entity_id: str, suffix: str, name: str | None = None, **config: Any) -> tuple[str, _Config]:
        return self._component("sensor", entity_id, name, state_topic=self._topic(suffix), device_class="temperature", unit_of_measurement="°C",
            state_class="measurement", suggested_display_precision=1, **config)

    def _temperature_number(self, entity_id: str, suffix: str, low: float, high: float, step: float = 0.1, name: str | None = None,
        **config: Any) -> tuple[str, _Config]:
        return self._component("number", entity_id, name, state_topic=self._topic(suffix), command_topic=self._topic(f"{suffix}/set"),
            device_class="temperature", unit_of_measurement="°C", min=low, max=high, step=step, mode="box", **config)

    def _switch(self, entity_id: str, suffix: str) -> tuple[str, _Config]:
        return self._component("switch", entity_id, state_topic=self._topic(suffix), command_topic=self._topic(f"{suffix}/set"), payload_on="ON",
            payload_off="OFF")

    def _central_components(self) -> list[tuple[str, _Config]]:
        numbers = (("central_setpoint", "setpoint", 5, 30, 0.5), ("central_demand_on_delta", "demandOnDelta", 0, 15, 0.1),
            ("central_demand_off_margin", "demandOffMargin", 0, 15, 0.1))
        return [self._switch("central_heat_demand", "central/demand"),
            *(self._temperature_number(entity_id, f"central/{suffix}", low, high, step, entity_category="config")
                for entity_id, suffix, low, high, step in numbers),
            self._component("sensor", "central_boiler_status", state_topic=self._topic("central/boilerStatus")),
            self._temperature_sensor("central_outside_temperature", "central/outsideTemperature")]

    def _flow_components(self) -> list[tuple[str, _Config]]:
        low, high = flow_temp_range(self._cfg.flow_curve)
        return [self._switch("flow_demand", "flow/demand"), self._temperature_number("flow_target", "flow/target", low, round(high, 1)),
            self._temperature_sensor("flow_outside_temperature", "flow/outsideTemperature", entity_category="diagnostic")]

    def _climate_components(self, zone: int) -> list[tuple[str, _Config]]:
        prefix, simple = f"z{zone}", self._is_simple_zone(zone)
        commands = {} if self._is_read_only_satellite_zone(zone) else dict(mode_command_topic=self._topic(f"{prefix}/mode/set"),
            preset_mode_command_topic=self._topic(f"{prefix}/preset/set"), temperature_command_topic=self._topic(f"{prefix}/targetTemperature/set"))
        presets = dict(preset_modes=list(SIMPLE_PRESET_MODES if simple else MQTT_PRESET_MODES)) if commands else {}
        climate = self._component("climate", f"zone{zone}_climate", self._label("zone_heating", zone=zone),
            modes=list(SIMPLE_HVAC_MODES if simple else HVAC_MODES), **presets,
            mode_state_topic=self._topic(f"{prefix}/mode"), preset_mode_state_topic=self._topic(f"{prefix}/preset"),
            action_topic=self._topic(f"{prefix}/action"), current_temperature_topic=self._topic(f"{prefix}/currentTemperature"),
            temperature_state_topic=self._topic(f"{prefix}/targetTemperature"),
            min_temp=CLIMATE_MIN_TEMP, max_temp=CLIMATE_MAX_TEMP, temp_step=CLIMATE_TEMP_STEP, temperature_unit="C", precision=0.1, **commands)
        if zone not in self._virtual: return [climate]
        # A virtual satellite reports the room temperature that Home Assistant feeds it.
        return [climate, self._temperature_number(f"zone{zone}_reported_ambient", f"{prefix}/reportedAmbient", CLIMATE_MIN_TEMP, 35,
            name=self._label("zone_reported_ambient", zone=zone))]

    def _components(self) -> dict[str, _Config]:
        "Every entity the current configuration exposes, as device-discovery components"
        components = []
        if self._boiler_entities_enabled():
            for entity_id, suffix, device_class, unit, kind in _BOILER_SENSORS:
                components.append(self._component("sensor", entity_id, state_topic=self._topic(f"boiler/{suffix}"), device_class=device_class,
                    unit_of_measurement=unit, suggested_display_precision=1 if unit in ("°C", "kW", "bar") else None, **_SENSOR_KINDS.get(kind, {})))
            dhw = dict(state_topic=self._topic("boiler/dhwMode"))
            if self._boiler_commands_enabled():
                components.append(self._component("select", "dhw_mode", command_topic=self._topic("boiler/dhwMode/set"),
                    options=[self._dhw_label(mode) for mode in DHW_SELECTABLE_MODES], **dhw))
            else: components.append(self._component("sensor", "dhw_mode", **dhw))
        if self._sonde_exposed():
            components.append(self._temperature_number("outside_temperature", "outsideSensor/outsideTemperature", SONDE_MIN_TEMPERATURE, SONDE_MAX_TEMPERATURE))
        for zone in (1, 2, 3):
            if not self._cfg.zone_enabled(zone): continue
            for key, suffix in (("flow_temperature", "flowTemperature"), ("flow_setpoint_temperature", "flowSetpointTemperature")):
                components.append(self._temperature_sensor(f"zone{zone}_{key}", f"z{zone}/{suffix}", self._label(f"zone_{key}", zone=zone)))
            if self._is_central_zone(zone): components += self._central_components()
            elif self._is_flow_zone(zone): components += self._flow_components()
            else: components += self._climate_components(zone)
        for operation in sorted(self._data.rf_operations):
            topic = self._topic(f"diagnostics/rf/{operation}")
            components.append(self._component("sensor", f"rf_{operation}", self._label("rf_operation", operation=operation.replace("_", " ")),
                state_topic=topic, value_template="{{ value_json.state }}", json_attributes_topic=topic, entity_category="diagnostic"))
        return dict(components)

    def _discovery(self, components: dict[str, _Config]) -> str:
        availability = [dict(topic=self.availability_topic(), payload_available="online", payload_not_available="offline")]
        return json.dumps(dict(dev=self._device_block(), o=self._origin_block(), availability=availability, cmps=components), sort_keys=True)

    async def publish_discovery(
        self,
        client: aiomqtt.Client,
        *,
        force: bool = False, # publish even if the config is unchanged, e.g. after Home Assistant restarts
    ) -> None:
        "Publish the device-discovery config if it changed, removing the entities the previous config had and this one lacks"
        components = self._components()
        # HA removes a component that is sent with only its platform; a later config then leaves it out.
        removed = {key: dict(p=platform) for key, platform in self._discovered.items() if key not in components}
        if removed: await client.publish(DEVICE_TOPIC, self._discovery(components | removed), retain=True)
        payload = self._discovery(components)
        if force or removed or payload != self._last_discovery:
            await client.publish(DEVICE_TOPIC, payload, retain=True)
            log.info("mqtt_discovery_published", entity_count=len(components), removed=sorted(removed))
        self._last_discovery, self._discovered = payload, {key: config["p"] for key, config in components.items()}
        await client.publish(self.availability_topic(), "online", retain=True)

    async def publish_offline(self, client: aiomqtt.Client) -> None: await client.publish(self.availability_topic(), "offline", retain=True)

    async def publish_state(self, client: aiomqtt.Client) -> None:
        # Several tasks publish. Serialising them keeps an older snapshot from landing after a newer one in HA's retained state.
        async with self._publish_lock: await self._publish_state(client)

    async def _publish_state(self, client: aiomqtt.Client) -> None:
        b = self._data.boiler
        mapping = {}
        if self._boiler_entities_enabled():
            mapping.update({"boiler/dhwTemperature": _fmt(b.dhw_temperature), "boiler/dhwInstantTemperature": _fmt(b.dhw_instant_temperature),
                "boiler/cdcTemperature": _fmt(b.cdc_temperature), "boiler/cdcSafetyTemperature": _fmt(b.cdc_safety_temperature),
                "boiler/flueTemperature": _fmt(b.flue_temperature), "boiler/dhwInstantPower": _fmt(b.dhw_power),
                "boiler/heatingInstantPower": _fmt(b.heating_power), "boiler/pressure": _fmt(b.pressure),
                "boiler/dhwConsumption": _fmt_int(b.dhw_consumption), "boiler/heatingConsumption": _fmt_int(b.heating_consumption),
                "boiler/status": self._boiler_status_label(b.status)})
            # The previous-day counters go out with the day they cover, so HA's statistics add each day exactly once.
            reset = daily_consumption_reset(b.daily_consumption_read_at)
            for suffix, value in (("dailyDhwConsumption", b.daily_dhw_consumption), ("dailyHeatingConsumption", b.daily_heating_consumption)):
                mapping[f"boiler/{suffix}"] = None if value is None or reset is None else json.dumps(dict(value=value, last_reset=reset))
            dhw_mode = self._visible_dhw_mode()
            mapping["boiler/dhwMode"] = self._dhw_label(dhw_mode) if dhw_mode else None
        if self._sonde_exposed(): mapping["outsideSensor/outsideTemperature"] = _fmt(self._data.sonde.outside_temperature)
        for zone, state in self._data.zones.items():
            if not self._cfg.zone_enabled(zone): continue
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
            if self._is_flow_zone(zone):
                mapping["flow/demand"] = _fmt_bool(state.flow_demand)
                mapping["flow/target"] = _fmt(state.flow_target)
                mapping["flow/outsideTemperature"] = _fmt(self._data.sonde.outside_temperature)
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
            else: mapping[f"{prefix}/currentTemperature"] = _fmt(state.ambient_temperature)
        for operation, health in sorted(self._data.rf_operations.items()):
            diagnostic = dict(operation=operation, state=health.state, fresh=health.is_fresh, attempts=health.attempts,
                successes=health.successes, nacks=health.nacks, timeouts=health.timeouts, transport_failures=health.transport_failures,
                consecutive_failures=health.consecutive_failures, last_rssi=health.last_rssi)
            if health.last_success is not None: diagnostic["last_success"] = health.last_success
            if health.other_failures: diagnostic["other_failures"] = health.other_failures
            if health.zone is not None:
                diagnostic["zone"] = health.zone
                diagnostic["source"] = self._data.zones[health.zone].source.value
            mapping[f'diagnostics/rf/{operation}'] = json.dumps(diagnostic, sort_keys=True)
        published_count = 0
        for suffix, value in mapping.items():
            if value is None or self._published_state.get(suffix) == value: continue
            await client.publish(self._topic(suffix), value, retain=True)
            self._published_state[suffix] = value
            published_count += 1
        if self._boiler_entities_enabled():
            dhw_mode = self._visible_dhw_mode()
            if dhw_mode != self._last_published_dhw_mode:
                self._last_published_dhw_mode = dhw_mode
                log.info('mqtt_dhw_state_published', mode=self._dhw_label(dhw_mode) if dhw_mode is not None else None,
                    pending=self._pending_dhw_mode is not None and time.monotonic() < self._pending_dhw_until)
        log.debug("mqtt_state_published", topic_count=published_count)

    async def handle_command(self, topic: str, payload: str) -> None:
        payload = payload.strip()
        log.info("mqtt_command_received", topic=topic, payload=payload)
        if topic.endswith("boiler/dhwMode/set"):
            if not self._boiler_commands_enabled(): raise RuntimeError("DHW mode writes require a paired [frisquet.connect] identity")
            mode = DhwMode.parse(payload)
            if self._pending_dhw_mode == mode and time.monotonic() < self._pending_dhw_until:
                log.debug("mqtt_command_skipped", topic=topic, reason="dhw_mode_already_pending", mode=mode.value)
                return
            assert self._ops is not None
            self._pending_dhw_mode = mode
            self._pending_dhw_until = time.monotonic() + DHW_PENDING_SECONDS
            if self._on_change: await self._on_change()

            try: await self._ops.write_dhw_mode(self._data, mode)
            except Exception:
                self._pending_dhw_mode = None
                self._pending_dhw_until = 0.0
                if self._on_change: await self._on_change()
                raise
            if self._on_change: await self._on_change()
        elif topic.endswith("outsideSensor/outsideTemperature/set"):
            temperature = float(payload)
            if self._cfg.flow_curve is not None: log.warning("mqtt_command_ignored", topic=topic, reason="flow_temp_owns_sonde")
            elif self._outside_temperature_writer is not None: await self._outside_temperature_writer(temperature)
            elif self._sonde_ops is not None:
                await self._sonde_ops.write_outside_temperature(self._data, temperature)
                if self._on_change: await self._on_change()
            else: log.warning("mqtt_command_ignored", topic=topic)
        else:
            profile_command = self._profile_command(topic)
            if profile_command is not None:
                profile, command = profile_command
                if profile == "central": await self._handle_central_command(command, payload)
                else: await self._handle_flow_command(command, payload)
                if self._on_change: await self._on_change()
                return
            zone_command = self._zone_command(topic)
            if zone_command is None:
                log.warning("mqtt_command_ignored", topic=topic)
                return
            zone, command = zone_command
            if self._is_zone1_profile(zone):
                log.warning("mqtt_command_ignored", topic=topic)
                return
            if zone in self._virtual: await self._handle_virtual_zone_command(zone, command, payload)
            elif self._is_read_only_satellite_zone(zone):
                log.warning("mqtt_command_ignored", topic=topic, reason="read_only_satellite_zone")
                return
            elif self._ops is not None: await self._handle_connect_zone_command(zone, command, payload)
            else:
                log.warning("mqtt_command_ignored", topic=topic)
                return
            if self._on_change: await self._on_change()

    async def handle_message(
        self,
        client: aiomqtt.Client,
        topic: str,
        payload: str,
        retain: bool = False, # the broker delivered a stored message, not a new one
    ) -> None:
        body = payload.strip()
        if topic == "homeassistant/status":
            if body == "online":
                self._ha_offline_since, self._falling_back = None, False
                self._published_state.clear()
                await self.publish_discovery(client, force=True)
                await self.publish_state(client)
                log.info("mqtt_homeassistant_birth_handled")
            elif body == "offline" and self._ha_offline_since is None: self._ha_offline_since = time.monotonic()
            return
        if topic == DEVICE_TOPIC:
            # The bridge's own config from its previous run lists the entities to remove if this run no longer has them.
            if retain and body:
                self._discovered = {key: config['p'] for key, config in json.loads(body).get('cmps', {}).items() if len(config) > 1}
                await self.publish_discovery(client)
            return
        if legacy := _LEGACY_DISCOVERY.fullmatch(topic):
            if retain and body and "migrate_discovery" not in body: await self._migrate(client, topic, f"{legacy[1]}_{legacy[2]}")
            return
        if retain:
            log.warning("mqtt_command_ignored", topic=topic, reason="retained")
            return
        await self.handle_command(topic, body)

    async def _migrate(self, client: aiomqtt.Client, topic: str, key: str) -> None:
        "Move a per-entity discovery config from before device discovery into the device config, keeping the entity in HA's registry"
        kept = key in self._components()
        if kept:
            await client.publish(topic, json.dumps(dict(migrate_discovery=True)), retain=True)
            await self.publish_discovery(client, force=True)
        await client.publish(topic, "", retain=True)
        log.info("mqtt_discovery_migrated", topic=topic, kept=kept)

    async def _handle_central_command(self, command: str, payload: str) -> None:
        if not self._is_central_zone(1):
            log.warning("mqtt_command_ignored", command=command)
            return
        zone = self._data.zones[1]
        if command == "demand": zone.central_demand = _parse_bool(payload)
        elif command == "setpoint": zone.central_setpoint = round(float(payload), 1)
        elif command == "demandOnDelta": zone.central_demand_on_delta = round(float(payload), 1)
        elif command == "demandOffMargin": zone.central_demand_off_margin = round(float(payload), 1)
        else:
            log.warning("mqtt_command_ignored", command=command)
            return
        self._persist_state()
        satellite = self._virtual.get(1)
        if satellite is not None: satellite.request_send()

    async def _handle_flow_command(self, command: str, payload: str) -> None:
        curve = self._cfg.flow_curve
        if curve is None:
            log.warning("mqtt_command_ignored", command=command)
            return
        if command == "demand": await self._set_flow(demand=_parse_bool(payload))
        else: await self._set_flow(target=float(payload))

    async def _set_flow(self, demand: bool | None = None, target: float | None = None) -> None:
        "Set zone 1's flow demand through its satellite and its flow target as a virtual outdoor temperature; None leaves either as it is"
        curve, zone = self._cfg.flow_curve, self._data.zones[1]
        if demand is not None: zone.flow_demand = demand
        if target is not None:
            low, high = flow_temp_range(curve)
            zone.flow_target = min(max(round(target, 1), low), high)
        self._persist_state()
        if demand is not None and (satellite := self._virtual.get(1)) is not None: satellite.request_send()
        if target is not None and self._outside_temperature_writer is not None:
            await self._outside_temperature_writer(flow_temp_outdoor(zone.flow_target, curve.slope))

    async def check_fallback(self, now: float | None = None) -> None:
        "Heat zone 1 at its fallback flow once Home Assistant's MQTT status has been offline for `FALLBACK_SECONDS`"
        zone_cfg = self._cfg.zone1
        fallback = zone_cfg.fallback_flow if zone_cfg is not None and self._cfg.flow_curve is not None else None
        if fallback is None or self._ha_offline_since is None or self._falling_back: return
        if (time.monotonic() if now is None else now)-self._ha_offline_since < FALLBACK_SECONDS: return
        self._falling_back = True
        log.warning("flow_fallback", flow=fallback)
        await self._set_flow(demand=True, target=fallback)

    async def _handle_connect_zone_command(
        self,
        zone: int,
        command: str, # mode, preset or targetTemperature
        payload: str,
    ) -> None:
        assert self._ops is not None
        if command == "mode": await self._ops.apply_zone_climate(zone, self._data, hvac_mode=payload)
        elif command == "preset": await self._ops.apply_zone_climate(zone, self._data, preset=payload)
        elif command == "targetTemperature": await self._ops.apply_zone_climate(zone, self._data, target_temperature=float(payload))
        else: log.warning("mqtt_command_ignored", zone=zone, command=command)

    async def _handle_virtual_zone_command(
        self,
        zone: int,
        command: str, # mode, preset, targetTemperature or reportedAmbient
        payload: str,
    ) -> None:
        satellite = self._virtual[zone]
        zs = self._data.zones[zone]
        if command == "reportedAmbient": zs.reported_ambient = float(payload)
        elif command == "mode":
            if self._is_simple_zone(zone): apply_simple_command(zs, hvac_mode=payload)
            else: apply_zone_intent(zs, resolve_zone_intent(zs, hvac_mode=payload))
        elif command == "preset":
            if self._is_simple_zone(zone): apply_simple_command(zs, preset=payload)
            else: apply_zone_intent(zs, resolve_zone_intent(zs, preset=payload))
        elif command == "targetTemperature":
            if self._is_simple_zone(zone): apply_simple_command(zs, target_temperature=float(payload))
            else: apply_zone_intent(zs, resolve_zone_intent(zs, target_temperature=float(payload)))
        else:
            log.warning("mqtt_command_ignored", zone=zone, command=command)
            return
        self._persist_state()
        satellite.request_send()

    def _persist_state(self) -> None:
        if self._on_persist_state is not None: self._on_persist_state()

    def _visible_dhw_mode(self) -> DhwMode | None:
        if self._pending_dhw_mode is None: return self._data.boiler.dhw_mode
        if time.monotonic() < self._pending_dhw_until: return self._pending_dhw_mode
        self._pending_dhw_mode = None
        self._pending_dhw_until = 0.0
        return self._data.boiler.dhw_mode

    def _profile_command(self, topic: str) -> tuple[str, str] | None:
        prefix = f"{self._base}/"
        if not topic.startswith(prefix): return None
        parts = topic[len(prefix) :].split("/")
        if len(parts) != 3 or parts[2] != "set" or parts[1] not in _PROFILE_COMMANDS.get(parts[0], ()): return None
        return parts[0], parts[1]

    def _zone_command(self, topic: str) -> tuple[int, str] | None:
        prefix = f"{self._base}/"
        if not topic.startswith(prefix): return None
        parts = topic[len(prefix) :].split("/")
        if len(parts) != 3 or parts[2] != "set": return None
        zone_part = parts[0]
        if len(zone_part) != 2 or zone_part[0] != "z" or not zone_part[1].isdigit(): return None
        zone = int(zone_part[1])
        if zone not in (1, 2, 3) or not self._cfg.zone_enabled(zone): return None
        if parts[1] not in {"mode", "preset", "targetTemperature", "reportedAmbient"}: return None
        return zone, parts[1]

    async def run(self, client: aiomqtt.Client) -> None:
        # Subscribe before publishing, so the broker's retained discovery configs from earlier runs arrive here.
        for topic_filter in (f"{self._base}/+/+/set", "homeassistant/status", DEVICE_TOPIC, f"homeassistant/+/{DEVICE_ID}/+/config"):
            await client.subscribe(topic_filter)
            log.info("mqtt_subscribed", topic_filter=topic_filter)
        await self.publish_discovery(client)
        await self.publish_state(client)
        async for message in client.messages:
            topic = str(message.topic)
            body = message.payload.decode("utf-8", errors="replace")
            try: await self.handle_message(client, topic, body, retain=message.retain)
            except Exception as exc: log.exception("mqtt_command_failed", topic=topic, error=str(exc))


def _fmt(value: float | None) -> str | None: return None if value is None else f"{value:.1f}"


def _fmt_int(value: int | None) -> str | None: return None if value is None else str(value)


def _fmt_bool(value: bool | None) -> str | None:
    if value is None: return None
    return "ON" if value else "OFF"


def _parse_bool(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "on", "yes", "heat"}: return True
    if normalized in {"0", "false", "off", "no", "idle"}: return False
    raise ValueError(f"expected boolean payload, got {value!r}")
