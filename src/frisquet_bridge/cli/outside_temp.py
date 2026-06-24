"""Send a virtual outside temperature reading."""

from __future__ import annotations

import argparse
import asyncio

from frisquet_bridge.cli.options import add_logging_options
from frisquet_bridge.config import ConfigError, load
from frisquet_bridge.connect.client import FrisquetClient
from frisquet_bridge.connect.ops import BoilerOps
from frisquet_bridge.connect.state import ProtocolState
from frisquet_bridge.frame import ADDR_SONDE
from frisquet_bridge.logging import RawMessageRecorder
from frisquet_bridge.model import BoilerData
from frisquet_bridge.transport.serial import SerialTransport


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "outside-temp",
        aliases=("set-outside-temp",),
        help="Send a virtual outside temperature reading",
    )
    p.add_argument("temperature", type=float, help="Temperature in °C")
    p.add_argument("--config", default="config.toml", help="Config file path")
    add_logging_options(p, suppress_default=True)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    try:
        asyncio.run(_send(args.config, args.temperature, args.raw_recorder))
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


async def _send(config_path: str, temperature: float, raw_recorder: RawMessageRecorder | None = None) -> None:
    cfg = load(config_path)
    if cfg.sonde is None:
        raise ConfigError("outside-temp requires [frisquet.sonde]")

    state = ProtocolState(**cfg.protocol_state_kwargs("sonde"))
    data = BoilerData()
    async with SerialTransport(cfg.serial.port, cfg.serial.speed, raw_recorder=raw_recorder) as transport:
        await transport.listen()
        client = FrisquetClient(transport, state, self_addr=ADDR_SONDE, boiler_addr=cfg.boiler_addr)
        ops = BoilerOps(client, boiler_addr=cfg.boiler_addr, memory_offset=cfg.memory_offset)
        await ops.write_outside_temperature(data, temperature)

    sent = data.sonde.outside_temperature
    print(f"Outside temperature sent: {sent:.1f}°C")
