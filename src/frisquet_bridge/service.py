"""Main asyncio service wiring transport, ops, scheduler, and optional MQTT."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import aiomqtt
import structlog

from frisquet_bridge.config import BridgeConfig
from frisquet_bridge.connect.client import FrisquetClient
from frisquet_bridge.connect.ops import BoilerOps
from frisquet_bridge.connect.state import ProtocolState
from frisquet_bridge.emulation import PassiveMirror
from frisquet_bridge.frame import ADDR_SATELLITE_Z1, ADDR_SATELLITE_Z2, ADDR_SATELLITE_Z3, ADDR_SONDE
from frisquet_bridge.logging import RawMessageRecorder
from frisquet_bridge.model import BoilerData
from frisquet_bridge.mqtt.adapter import DEVICE_ID, MqttAdapter
from frisquet_bridge.satellite import VirtualSatellite
from frisquet_bridge.scheduler import PollScheduler
from frisquet_bridge.state_store import load_zone_state, save_zone_state
from frisquet_bridge.transport.serial import SerialTransport

log = structlog.get_logger(__name__)

_SATELLITE_ADDR = {1: ADDR_SATELLITE_Z1, 2: ADDR_SATELLITE_Z2, 3: ADDR_SATELLITE_Z3}


class BridgeService:
    def __init__(self, cfg: BridgeConfig, *, raw_recorder: RawMessageRecorder | None = None) -> None:
        self.cfg = cfg
        self.data = BoilerData()
        self._raw_recorder = raw_recorder
        self._stop = asyncio.Event()

    def stop(self) -> None:
        log.info("service_stop_requested")
        self._stop.set()

    async def run(self) -> None:
        log.info(
            "service_starting",
            serial_port=self.cfg.serial.port,
            serial_speed=self.cfg.serial.speed,
            mqtt_enabled=self.cfg.mqtt.enabled,
            mqtt_host=self.cfg.mqtt.host if self.cfg.mqtt.enabled else None,
            mqtt_port=self.cfg.mqtt.port if self.cfg.mqtt.enabled else None,
            connect_mode=self.cfg.connect.mode if self.cfg.connect is not None else None,
            connect_reads=self.cfg.connect_reads_enabled,
            sensor_poll_interval_seconds=self.cfg.sensor_poll_interval_seconds,
            connect_identity=self.cfg.connect is not None and self.cfg.connect.identity is not None,
            sonde_enabled=self.cfg.sonde is not None and self.cfg.sonde.enabled,
        )
        transport = SerialTransport(self.cfg.serial.port, self.cfg.serial.speed, raw_recorder=self._raw_recorder)
        # One lock shared by every client so connect/sonde/satellite exchanges
        # never transmit over each other on the half-duplex modem.
        request_lock = asyncio.Lock()
        connect_ops: BoilerOps | None = None
        sonde_ops: BoilerOps | None = None

        if self.cfg.connect is not None and self.cfg.connect.identity is not None:
            state = ProtocolState(**self.cfg.protocol_state_kwargs("connect"))
            client = FrisquetClient(
                transport,
                state,
                boiler_addr=self.cfg.boiler_addr,
                lock=request_lock,
            )
            connect_ops = BoilerOps(client, boiler_addr=self.cfg.boiler_addr, memory_offset=self.cfg.memory_offset)

        if self.cfg.sonde is not None:
            sonde_state = ProtocolState(**self.cfg.protocol_state_kwargs("sonde"))
            sonde_client = FrisquetClient(
                transport,
                sonde_state,
                self_addr=ADDR_SONDE,
                boiler_addr=self.cfg.boiler_addr,
                lock=request_lock,
            )
            sonde_ops = BoilerOps(sonde_client, boiler_addr=self.cfg.boiler_addr, memory_offset=self.cfg.memory_offset)

        satellite_ops: dict[int, BoilerOps] = {}
        for zone_number in (1, 2, 3):
            zone_cfg = self.cfg.zone(zone_number)
            if zone_cfg is None or not zone_cfg.uses_virtual_satellite:
                continue
            sat_state = ProtocolState(**self.cfg.protocol_state_kwargs(f"satellite_z{zone_number}"))
            sat_client = FrisquetClient(
                transport,
                sat_state,
                self_addr=_SATELLITE_ADDR[zone_number],
                boiler_addr=self.cfg.boiler_addr,
                lock=request_lock,
            )
            satellite_ops[zone_number] = BoilerOps(sat_client, boiler_addr=self.cfg.boiler_addr, memory_offset=self.cfg.memory_offset)

        if self.cfg.connect_reads_enabled and connect_ops is None:
            log.warning(
                "boiler_polling_unavailable",
                reason='connect.mode = "read" or "full" requires a paired connect identity for active memory polling',
            )

        async with AsyncExitStack() as stack:
            await stack.enter_async_context(transport)
            await transport.listen()

            mqtt_client: aiomqtt.Client | None = None
            adapter: MqttAdapter | None = None

            async def publish_mqtt() -> None:
                if adapter is not None and mqtt_client is not None:
                    await adapter.publish_state(mqtt_client)

            enabled_zones = tuple(zone for zone in (1, 2, 3) if self.cfg.zone_enabled(zone))

            virtual_satellites: dict[int, VirtualSatellite] = {
                zone_number: VirtualSatellite(
                    zone_number,
                    satellite_ops[zone_number],
                    self.data,
                    profile=(self.cfg.zone(zone_number).mode if self.cfg.zone(zone_number) is not None else "virtual_satellite"),
                    on_update=publish_mqtt,
                )
                for zone_number in satellite_ops
            }

            mirror = None
            if self.cfg.connect is not None or enabled_zones:
                load_zone_state(self.cfg.state_path, self.data)
                mirror = PassiveMirror(
                    self.data,
                    boiler_addr=self.cfg.boiler_addr,
                    on_update=publish_mqtt,
                    on_zone_config=lambda: save_zone_state(self.cfg.state_path, self.data),
                )

            if self.cfg.mqtt.enabled:
                availability_topic = f"{self.cfg.mqtt.base_topic.rstrip('/')}/{DEVICE_ID}/availability"
                mqtt_client = aiomqtt.Client(
                    hostname=self.cfg.mqtt.host,
                    port=self.cfg.mqtt.port,
                    username=self.cfg.mqtt.username or None,
                    password=self.cfg.mqtt.password or None,
                    identifier=self.cfg.mqtt.client_id,
                    will=aiomqtt.Will(availability_topic, "offline", retain=True),
                )
                log.info("mqtt_connecting", host=self.cfg.mqtt.host, port=self.cfg.mqtt.port, client_id=self.cfg.mqtt.client_id)
                await stack.enter_async_context(mqtt_client)
                log.info("mqtt_connected", host=self.cfg.mqtt.host, port=self.cfg.mqtt.port, client_id=self.cfg.mqtt.client_id)
                adapter = MqttAdapter(
                    self.cfg,
                    self.data,
                    connect_ops,
                    sonde_ops=sonde_ops,
                    virtual_satellites=virtual_satellites,
                    on_state_change=publish_mqtt,
                    on_persist_state=lambda: save_zone_state(self.cfg.state_path, self.data),
                )
                await adapter.publish_discovery(mqtt_client)

            poll_connect = connect_ops is not None and self.cfg.connect_reads_enabled
            scheduler = PollScheduler(
                connect_ops,
                self.data,
                poll_connect=poll_connect,
                sonde_ops=sonde_ops,
                push_outside_temperature=self.cfg.sonde is not None and self.cfg.sonde.enabled,
                sensor_interval=self.cfg.sensor_poll_interval_seconds,
                enabled_zones=enabled_zones,
                on_update=publish_mqtt,
            )

            async def rf_loop() -> None:
                async for received in transport.frames():
                    if mirror is not None:
                        await mirror.handle(received)
                        await mirror.notify()
                    if self._stop.is_set():
                        break

            async def mqtt_loop() -> None:
                if adapter is None or mqtt_client is None:
                    return
                await adapter.run(mqtt_client)

            tasks = [
                asyncio.create_task(scheduler.run(), name="scheduler"),
                asyncio.create_task(rf_loop(), name="rf"),
            ]
            if adapter is not None:
                tasks.append(asyncio.create_task(mqtt_loop(), name="mqtt"))
            for zone_number, satellite in virtual_satellites.items():
                tasks.append(asyncio.create_task(satellite.run(), name=f"satellite_z{zone_number}"))
            for task in tasks:
                task.add_done_callback(self._log_task_done)

            log.info("service_started", tasks=[task.get_name() for task in tasks])
            await self._stop.wait()
            scheduler.stop()
            for satellite in virtual_satellites.values():
                satellite.stop()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if adapter is not None and mqtt_client is not None:
                await adapter.publish_offline(mqtt_client)
            log.info("service_stopped")

    def _log_task_done(self, task: asyncio.Task[object]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            log.info("service_task_finished", task=task.get_name())
            return
        log.error("service_task_failed", task=task.get_name(), exc_info=(type(exc), exc, exc.__traceback__))
        self.stop()
