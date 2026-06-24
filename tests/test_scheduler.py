"""Tests for periodic service scheduling."""

from __future__ import annotations

import asyncio

from frisquet_bridge.model import BoilerData
from frisquet_bridge.scheduler import PollScheduler


class FakeSondeOps:
    def __init__(self) -> None:
        self.temperatures: list[float] = []
        self._second_write = asyncio.Event()

    async def write_outside_temperature(self, data: BoilerData, temperature: float) -> None:
        self.temperatures.append(temperature)
        data.sonde.outside_temperature = temperature
        if len(self.temperatures) >= 2:
            self._second_write.set()

    async def wait_for_second_write(self) -> None:
        await asyncio.wait_for(self._second_write.wait(), timeout=1.0)


class FakeBoilerOps:
    def __init__(self) -> None:
        self.sensor_reads = 0
        self._second_sensor_read = asyncio.Event()

    async def read_sensors(self, data: BoilerData) -> None:
        self.sensor_reads += 1
        if self.sensor_reads >= 2:
            self._second_sensor_read.set()

    async def read_satellite_info(self, data: BoilerData) -> None:
        pass

    async def read_consumption(self, data: BoilerData) -> None:
        pass

    async def read_daily_consumption(self, data: BoilerData) -> None:
        pass

    async def read_dhw_mode(self, data: BoilerData) -> None:
        pass

    async def read_clock(self, data: BoilerData) -> None:
        pass

    async def wait_for_second_sensor_read(self) -> None:
        await asyncio.wait_for(self._second_sensor_read.wait(), timeout=1.0)


async def test_scheduler_uses_configured_sensor_interval() -> None:
    data = BoilerData()
    ops = FakeBoilerOps()
    scheduler = PollScheduler(
        ops,  # type: ignore[arg-type]
        data,
        sensor_interval=0.01,
    )

    task = asyncio.create_task(scheduler.run())
    try:
        await ops.wait_for_second_sensor_read()
    finally:
        scheduler.stop()
        await task

    assert ops.sensor_reads >= 2


async def test_scheduler_repushes_outside_temperature_even_when_unchanged() -> None:
    data = BoilerData()
    data.sonde.outside_temperature = 12.3
    sonde_ops = FakeSondeOps()
    scheduler = PollScheduler(
        None,
        data,
        poll_connect=False,
        sonde_ops=sonde_ops,  # type: ignore[arg-type]
        push_outside_temperature=True,
        outside_temperature_interval=0.01,
    )

    task = asyncio.create_task(scheduler.run())
    try:
        await sonde_ops.wait_for_second_write()
    finally:
        scheduler.stop()
        await task

    assert sonde_ops.temperatures[:2] == [12.3, 12.3]


async def test_scheduler_does_not_push_missing_outside_temperature() -> None:
    data = BoilerData()
    sonde_ops = FakeSondeOps()
    scheduler = PollScheduler(
        None,
        data,
        poll_connect=False,
        sonde_ops=sonde_ops,  # type: ignore[arg-type]
        push_outside_temperature=True,
        outside_temperature_interval=0.01,
    )

    task = asyncio.create_task(scheduler.run())
    try:
        await asyncio.sleep(0.02)
    finally:
        scheduler.stop()
        await task

    assert sonde_ops.temperatures == []
