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
SATELLITE_INFO_INTERVAL = 600.0
SATELLITE_INFO_TIMEOUT = 1.0
OUTSIDE_TEMPERATURE_INTERVAL = 600.0
OUTSIDE_TEMPERATURE_RETRY_INTERVAL = 60.0
SLOW_INTERVAL = 3600.0

_UNCHANGED = object()


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
        poll_satellite_info: bool = True,
        satellite_info_interval: float = SATELLITE_INFO_INTERVAL,
        satellite_info_timeout: float = SATELLITE_INFO_TIMEOUT,
        outside_temperature_interval: float = OUTSIDE_TEMPERATURE_INTERVAL,
        outside_temperature_retry_interval: float = OUTSIDE_TEMPERATURE_RETRY_INTERVAL,
        enabled_zones: tuple[int, ...] = (1, 2, 3),
        on_update: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._ops = ops
        self._data = data
        self._poll_connect = poll_connect
        self._sonde_ops = sonde_ops
        self._push_outside_temperature = push_outside_temperature
        self._sensor_interval = sensor_interval
        self._poll_satellite_info = poll_satellite_info
        self._satellite_info_interval = satellite_info_interval
        self._satellite_info_timeout = satellite_info_timeout
        self._outside_temperature_interval = outside_temperature_interval
        self._outside_temperature_retry_interval = outside_temperature_retry_interval
        self._outside_temperature_due = 0.0
        self._outside_temperature_lock = asyncio.Lock()
        self._enabled_zones = enabled_zones
        self._on_update = on_update
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def send_outside_temperature_now(self, temperature: float) -> bool:
        """Store and immediately send a new desired outside temperature."""
        normalized = max(-30.0, min(80.0, round(temperature * 10) / 10))
        return await self._attempt_outside_temperature(normalized)

    async def _attempt_outside_temperature(
        self,
        temperature: float | object = _UNCHANGED,
        *,
        only_if_due: bool = False,
    ) -> bool:
        if self._sonde_ops is None:
            return False
        async with self._outside_temperature_lock:
            loop = asyncio.get_running_loop()
            if only_if_due and loop.time() < self._outside_temperature_due:
                return False
            if temperature is not _UNCHANGED:
                self._data.sonde.outside_temperature = float(temperature)
            desired = self._data.sonde.outside_temperature
            if desired is None:
                return False
            succeeded = await self._safe_poll(
                "outside_temperature",
                lambda: self._sonde_ops.write_outside_temperature(self._data, desired),
            )
            interval = self._outside_temperature_interval if succeeded else self._outside_temperature_retry_interval
            self._outside_temperature_due = asyncio.get_running_loop().time() + interval
            self._wake.set()
            return succeeded

    async def run(self) -> None:
        sensor_due = 0.0
        satellite_info_due = 0.0
        slow_due = 0.0
        loop = asyncio.get_running_loop()

        while not self._stop.is_set():
            self._wake.clear()
            if self._stop.is_set():
                break
            now = loop.time()
            if self._poll_connect and self._ops is not None and now >= sensor_due:
                await self._safe_poll("sensors", lambda: self._ops.read_sensors(self._data))
                sensor_due = now + self._sensor_interval
                self._log_state()
            if self._poll_connect and self._ops is not None and now >= satellite_info_due:
                ops = self._ops
                await self._safe_poll(
                    "satellite_info",
                    lambda ops=ops: ops.read_satellite_info(
                        self._data,
                        timeout=self._satellite_info_timeout,
                        retries=1,
                    ),
                )
                satellite_info_due = float("inf")
                if self._poll_satellite_info:
                    satellite_info_due = now + self._satellite_info_interval
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
                and now >= self._outside_temperature_due
            ):
                await self._attempt_outside_temperature(only_if_due=True)
            next_due = []
            if self._poll_connect and self._ops is not None:
                next_due.extend((sensor_due, satellite_info_due, slow_due))
            if self._push_outside_temperature and self._sonde_ops is not None and self._data.sonde.outside_temperature is not None:
                next_due.append(self._outside_temperature_due)
            sleep_for = 5.0
            if next_due:
                sleep_for = min(sleep_for, max(0.0, min(next_due) - loop.time()))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=sleep_for)

    async def _safe_poll(self, name: str, fn: Callable[[], Awaitable[None]]) -> bool:
        succeeded = False
        try:
            await fn()
        except Exception:
            log.exception("poll_failed", poll=name)
        else:
            succeeded = True
            log.debug("poll_succeeded", poll=name)
        if self._on_update is not None:
            try:
                await self._on_update()
            except Exception:
                log.exception("poll_update_failed", poll=name)
        return succeeded

    def _log_state(self) -> None:
        """Log the current internal state (zone temps, modes, boiler) for debugging."""
        d = self._data
        for z in self._enabled_zones:
            zs = d.zones[z]
            log.info(
                "zone_state",
                zone=z,
                ambient_temperature=zs.ambient_temperature,
                reported_ambient=zs.reported_ambient,
                mode=zs.mode.value if zs.mode is not None else None,
                setpoint_temperature=zs.setpoint_temperature,
                flow_temperature=zs.flow_temperature,
                flow_setpoint_temperature=zs.flow_setpoint_temperature,
                comfort_temperature=zs.comfort_temperature,
                reduced_temperature=zs.reduced_temperature,
                frost_temperature=zs.frost_temperature,
                override=zs.override,
                boost=zs.boost,
            )
        log.info(
            "boiler_state",
            outside_temperature=d.sonde.outside_temperature,
            boiler_status=d.boiler.status.value if d.boiler.status is not None else None,
            boiler_fault=d.boiler.fault,
            dhw_temperature=d.boiler.dhw_temperature,
        )
