"""Tests for periodic service scheduling."""

from __future__ import annotations

import asyncio

import pytest

from frisquet_bridge.model import BoilerData
from frisquet_bridge.scheduler import (
    OUTSIDE_TEMPERATURE_INTERVAL,
    OUTSIDE_TEMPERATURE_RETRY_INTERVAL,
    PollScheduler,
)


class FakeSondeOps:
    def __init__(self, *, fail_attempts: set[int] | None = None) -> None:
        self.temperatures: list[float] = []
        self.fail_attempts = fail_attempts or set()
        self._first_write = asyncio.Event()
        self._second_write = asyncio.Event()

    async def write_outside_temperature(self, data: BoilerData, temperature: float) -> None:
        self.temperatures.append(temperature)
        attempt = len(self.temperatures)
        self._first_write.set()
        if attempt >= 2:
            self._second_write.set()
        if attempt in self.fail_attempts:
            raise RuntimeError("RF write failed")
        data.sonde.outside_temperature = temperature

    async def wait_for_first_write(self) -> None:
        await asyncio.wait_for(self._first_write.wait(), timeout=1.0)

    async def wait_for_second_write(self) -> None:
        await asyncio.wait_for(self._second_write.wait(), timeout=1.0)


class FakeBoilerOps:
    def __init__(self) -> None:
        self.sensor_reads = 0
        self.satellite_info_reads = 0
        self.satellite_info_budgets: list[tuple[float, int]] = []
        self._second_sensor_read = asyncio.Event()
        self._second_satellite_info_read = asyncio.Event()

    async def read_sensors(self, data: BoilerData) -> None:
        self.sensor_reads += 1
        if self.sensor_reads >= 2:
            self._second_sensor_read.set()

    async def read_satellite_info(
        self,
        data: BoilerData,
        *,
        timeout: float = 5.0,
        retries: int = 3,
    ) -> None:
        self.satellite_info_reads += 1
        self.satellite_info_budgets.append((timeout, retries))
        if self.satellite_info_reads >= 2:
            self._second_satellite_info_read.set()

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

    async def wait_for_second_satellite_info_read(self) -> None:
        await asyncio.wait_for(self._second_satellite_info_read.wait(), timeout=1.0)


class FailingSatelliteInfoOps(FakeBoilerOps):
    async def read_satellite_info(
        self,
        data: BoilerData,
        *,
        timeout: float = 5.0,
        retries: int = 3,
    ) -> None:
        self.satellite_info_reads += 1
        self.satellite_info_budgets.append((timeout, retries))
        raise RuntimeError("no A029 response")


async def test_failed_rf_operation_still_publishes_health_update() -> None:
    data = BoilerData()
    published = 0

    async def fail_poll() -> None:
        raise RuntimeError("RF failed")

    async def publish() -> None:
        nonlocal published
        published += 1

    scheduler = PollScheduler(None, data, poll_connect=False, on_update=publish)

    assert await scheduler._safe_poll("test", fail_poll) is False
    assert published == 1


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


async def test_scheduler_bootstraps_satellite_info_once_without_physical_zones() -> None:
    data = BoilerData()
    ops = FakeBoilerOps()
    scheduler = PollScheduler(
        ops,  # type: ignore[arg-type]
        data,
        sensor_interval=0.01,
        poll_satellite_info=False,
    )

    task = asyncio.create_task(scheduler.run())
    try:
        await ops.wait_for_second_sensor_read()
    finally:
        scheduler.stop()
        await task

    assert ops.satellite_info_reads == 1
    assert ops.satellite_info_budgets == [(1.0, 1)]


async def test_scheduler_polls_satellite_info_at_its_own_interval_for_physical_zones() -> None:
    data = BoilerData()
    ops = FakeBoilerOps()
    scheduler = PollScheduler(
        ops,  # type: ignore[arg-type]
        data,
        sensor_interval=1.0,
        poll_satellite_info=True,
        satellite_info_interval=0.01,
    )

    task = asyncio.create_task(scheduler.run())
    try:
        await ops.wait_for_second_satellite_info_read()
    finally:
        scheduler.stop()
        await task

    assert ops.sensor_reads == 1
    assert ops.satellite_info_reads >= 2


async def test_satellite_info_failure_does_not_stop_sensor_polling() -> None:
    data = BoilerData()
    ops = FailingSatelliteInfoOps()
    scheduler = PollScheduler(
        ops,  # type: ignore[arg-type]
        data,
        sensor_interval=0.01,
        poll_satellite_info=False,
    )

    task = asyncio.create_task(scheduler.run())
    try:
        await ops.wait_for_second_sensor_read()
    finally:
        scheduler.stop()
        await task

    assert ops.satellite_info_reads == 1
    assert ops.satellite_info_budgets == [(1.0, 1)]
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


def test_outside_temperature_uses_ten_minute_keepalive_and_one_minute_retry() -> None:
    assert OUTSIDE_TEMPERATURE_INTERVAL == 600.0
    assert OUTSIDE_TEMPERATURE_RETRY_INTERVAL == 60.0


async def test_failed_outside_temperature_write_retries_on_short_interval() -> None:
    data = BoilerData()
    data.sonde.outside_temperature = 12.3
    sonde_ops = FakeSondeOps(fail_attempts={1})
    scheduler = PollScheduler(
        None,
        data,
        poll_connect=False,
        sonde_ops=sonde_ops,  # type: ignore[arg-type]
        push_outside_temperature=True,
        outside_temperature_interval=1.0,
        outside_temperature_retry_interval=0.01,
    )

    task = asyncio.create_task(scheduler.run())
    try:
        await sonde_ops.wait_for_second_write()
    finally:
        scheduler.stop()
        await task

    assert sonde_ops.temperatures[:2] == [12.3, 12.3]


async def test_immediate_success_resets_keepalive_without_duplicate_send() -> None:
    data = BoilerData()
    sonde_ops = FakeSondeOps()
    scheduler = PollScheduler(
        None,
        data,
        poll_connect=False,
        sonde_ops=sonde_ops,  # type: ignore[arg-type]
        push_outside_temperature=True,
        outside_temperature_interval=0.05,
        outside_temperature_retry_interval=1.0,
    )

    task = asyncio.create_task(scheduler.run())
    try:
        await asyncio.sleep(0)
        assert await scheduler.send_outside_temperature_now(12.34) is True
        assert sonde_ops.temperatures == [12.3]
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(sonde_ops._second_write.wait(), timeout=0.01)
        await sonde_ops.wait_for_second_write()
    finally:
        scheduler.stop()
        await task

    assert sonde_ops.temperatures[:2] == [12.3, 12.3]


async def test_successful_rf_write_is_not_retried_when_state_publication_fails() -> None:
    data = BoilerData()
    sonde_ops = FakeSondeOps()

    async def fail_publish() -> None:
        raise RuntimeError("MQTT unavailable")

    scheduler = PollScheduler(
        None,
        data,
        poll_connect=False,
        sonde_ops=sonde_ops,  # type: ignore[arg-type]
        push_outside_temperature=True,
        outside_temperature_interval=1.0,
        outside_temperature_retry_interval=0.01,
        on_update=fail_publish,
    )

    assert await scheduler.send_outside_temperature_now(12.3) is True
    task = asyncio.create_task(scheduler.run())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(sonde_ops._second_write.wait(), timeout=0.03)
    finally:
        scheduler.stop()
        await task

    assert sonde_ops.temperatures == [12.3]


async def test_immediate_failed_write_preserves_value_for_retry() -> None:
    data = BoilerData()
    sonde_ops = FakeSondeOps(fail_attempts={1})
    scheduler = PollScheduler(
        None,
        data,
        poll_connect=False,
        sonde_ops=sonde_ops,  # type: ignore[arg-type]
        push_outside_temperature=True,
        outside_temperature_interval=1.0,
        outside_temperature_retry_interval=0.01,
    )

    task = asyncio.create_task(scheduler.run())
    try:
        await asyncio.sleep(0)
        assert await scheduler.send_outside_temperature_now(12.34) is False
        assert data.sonde.outside_temperature == 12.3
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
