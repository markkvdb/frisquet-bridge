"""Async polling scheduler for boiler reads."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import structlog

from frisquet_bridge.connect.ops import BoilerOps
from frisquet_bridge.model import BoilerData

log = structlog.get_logger(__name__)

SENSOR_INTERVAL = 30.0
SLOW_INTERVAL = 3600.0


class PollScheduler:
    def __init__(
        self,
        ops: BoilerOps | None,
        data: BoilerData,
        *,
        poll_connect: bool = True,
        sonde_ops: BoilerOps | None = None,
        push_outside_temperature: bool = False,
        sensor_interval: float = SENSOR_INTERVAL,
        outside_temperature_interval: float = SENSOR_INTERVAL,
        enabled_zones: tuple[int, ...] = (1, 2, 3),
        on_update: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._ops = ops
        self._data = data
        self._poll_connect = poll_connect
        self._sonde_ops = sonde_ops
        self._push_outside_temperature = push_outside_temperature
        self._sensor_interval = sensor_interval
        self._outside_temperature_interval = outside_temperature_interval
        self._enabled_zones = enabled_zones
        self._on_update = on_update
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        sensor_due = 0.0
        slow_due = 0.0
        outside_temperature_due = 0.0
        loop = asyncio.get_running_loop()

        while not self._stop.is_set():
            now = loop.time()
            if self._poll_connect and self._ops is not None and now >= sensor_due:
                await self._safe_poll("sensors", lambda: self._ops.read_sensors(self._data))
                # Zone mode/ambient/setpoint come from satellite_info (0xa029).
                # Zone schedule + comfort/reduced/frost setpoints are learned
                # passively from satellite 0xa154 broadcasts (see PassiveMirror);
                # the boiler does not serve a zone-config read to Connect.
                await self._safe_poll("satellite_info", lambda: self._ops.read_satellite_info(self._data))
                sensor_due = now + self._sensor_interval
            if self._poll_connect and self._ops is not None and now >= slow_due:
                await self._safe_poll("consumption", lambda: self._ops.read_consumption(self._data))
                await self._safe_poll("daily_consumption", lambda: self._ops.read_daily_consumption(self._data))
                await self._safe_poll("dhw_mode", lambda: self._ops.read_dhw_mode(self._data))
                await self._safe_poll("clock", lambda: self._ops.read_clock(self._data))
                slow_due = now + SLOW_INTERVAL
            if (
                self._push_outside_temperature
                and self._sonde_ops is not None
                and self._data.sonde.outside_temperature is not None
                and now >= outside_temperature_due
            ):
                temperature = self._data.sonde.outside_temperature
                await self._safe_poll(
                    "outside_temperature",
                    lambda temperature=temperature: self._sonde_ops.write_outside_temperature(self._data, temperature),
                )
                outside_temperature_due = now + self._outside_temperature_interval
            next_due = []
            if self._poll_connect and self._ops is not None:
                next_due.extend((sensor_due, slow_due))
            if self._push_outside_temperature and self._sonde_ops is not None and self._data.sonde.outside_temperature is not None:
                next_due.append(outside_temperature_due)
            sleep_for = 5.0
            if next_due:
                sleep_for = min(sleep_for, max(0.0, min(next_due) - loop.time()))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)

    async def _safe_poll(self, name: str, fn: Callable[[], Awaitable[None]]) -> None:
        try:
            await fn()
            log.debug("poll_succeeded", poll=name)
            if self._on_update is not None:
                await self._on_update()
        except Exception:
            log.exception("poll_failed", poll=name)
