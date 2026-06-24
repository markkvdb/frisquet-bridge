"""One-shot read of boiler state."""

from __future__ import annotations

import argparse
import asyncio

from frisquet_bridge.cli.options import add_logging_options
from frisquet_bridge.config import BridgeConfig, ConfigError, load
from frisquet_bridge.connect.client import FrisquetClient
from frisquet_bridge.connect.ops import BoilerOps
from frisquet_bridge.connect.state import ProtocolState
from frisquet_bridge.logging import RawMessageRecorder
from frisquet_bridge.model import BoilerData
from frisquet_bridge.transport.serial import SerialTransport


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("read", help="Read boiler state once and print")
    p.add_argument("--config", default="config.toml", help="Config file path")
    add_logging_options(p, suppress_default=True)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    try:
        asyncio.run(_read(args.config, args.raw_recorder))
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


async def _read(config_path: str, raw_recorder: RawMessageRecorder | None = None) -> None:
    cfg = load(config_path)
    _require_connect_reader(cfg)
    state = ProtocolState(**cfg.protocol_state_kwargs("connect"))
    data = BoilerData()
    async with SerialTransport(cfg.serial.port, cfg.serial.speed, raw_recorder=raw_recorder) as transport:
        await transport.listen()
        client = FrisquetClient(transport, state, boiler_addr=cfg.boiler_addr)
        ops = BoilerOps(client, boiler_addr=cfg.boiler_addr, memory_offset=cfg.memory_offset)
        await ops.read_sensors(data)
        await ops.read_consumption(data)
        await ops.read_daily_consumption(data)
        await ops.read_dhw_mode(data)
        await ops.read_clock(data)
    b = data.boiler
    print(f"Boiler clock: {data.last_seen_date or 'n/a'}")
    print(
        f"DHW: {_fmt(b.dhw_temperature, '°C')}  "
        f"DHW instant: {_fmt(b.dhw_instant_temperature, '°C')}  "
        f"CDC: {_fmt(b.cdc_temperature, '°C')}  "
        f"CDC safety: {_fmt(b.cdc_safety_temperature, '°C')}"
    )
    print(
        f"Flue: {_fmt(b.flue_temperature, '°C')}  "
        f"Outside: {_fmt(b.outside_temperature, '°C')}  "
        f"Pressure: {_fmt(b.pressure, ' bar')}"
    )
    print(f"DHW power: {_fmt(b.dhw_power, ' kW')}  Heating: {_fmt(b.heating_power, ' kW')}")
    print(f"Total consumption DHW: {b.dhw_consumption} kWh  Heating: {b.heating_consumption} kWh")
    print(f"Daily consumption DHW: {b.daily_dhw_consumption} kWh  Heating: {b.daily_heating_consumption} kWh")
    print(f"DHW mode: {b.dhw_mode}")
    enabled_zones = {
        1: cfg.zone_enabled(1),
        2: cfg.zone_enabled(2),
        3: cfg.zone_enabled(3),
    }
    for n, z in data.zones.items():
        if not enabled_zones[n]:
            continue
        print(
            f"Zone {n}: "
            f"mode {_zone_mode(z)}  "
            f"ambient {_fmt(z.ambient_temperature, '°C')}  "
            f"setpoint {_fmt(z.setpoint_temperature, '°C')}  "
            f"flow {_fmt(z.flow_temperature, '°C')}  "
            f"flow setpoint {_fmt(z.flow_setpoint_temperature, '°C')}"
        )
        if z.schedule is not None:
            print(f"Zone {n} schedule raw: {z.schedule.compact_hex()}")


def _require_connect_reader(cfg: BridgeConfig) -> None:
    if not cfg.connect_reads_enabled:
        raise ConfigError('read requires [frisquet.connect] mode = "read" or "full" with a paired identity')


def _fmt(value: object, unit: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.1f}{unit}"
    return f"{value}{unit}"


def _zone_mode(zone: object) -> str:
    mode = getattr(zone, "mode", None)
    if mode is None:
        return "n/a"
    label = mode.value
    if getattr(zone, "boost", False):
        label += " + Boost"
    auto_comfort = getattr(zone, "auto_comfort", None)
    if auto_comfort is not None:
        label += f" ({'comfort' if auto_comfort else 'reduced'})"
    if getattr(zone, "override", False):
        label += " override"
    return label
