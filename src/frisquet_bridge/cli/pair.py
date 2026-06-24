"""Pair with the boiler and persist network/association ids."""

from __future__ import annotations

import argparse
import asyncio

from frisquet_bridge.cli.options import add_logging_options
from frisquet_bridge.config import load
from frisquet_bridge.connect.client import FrisquetClient
from frisquet_bridge.connect.state import ProtocolState
from frisquet_bridge.frame import ADDR_CONNECT, ADDR_SATELLITE_Z1, ADDR_SATELLITE_Z2, ADDR_SATELLITE_Z3, ADDR_SONDE
from frisquet_bridge.logging import RawMessageRecorder
from frisquet_bridge.transport.serial import SerialTransport

PAIR_ROLES = {
    "connect": ADDR_CONNECT,
    "sonde": ADDR_SONDE,
    "satellite_z1": ADDR_SATELLITE_Z1,
    "satellite_z2": ADDR_SATELLITE_Z2,
    "satellite_z3": ADDR_SATELLITE_Z3,
}


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("pair", help="Pair with the boiler (association mode)")
    p.add_argument("role", nargs="?", default="connect", choices=tuple(PAIR_ROLES), help="Device identity to pair")
    p.add_argument("--config", default="config.toml", help="Config file path")
    add_logging_options(p, suppress_default=True)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    try:
        asyncio.run(_pair(args.config, args.role, args.raw_recorder))
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


async def _pair(config_path: str, role: str, raw_recorder: RawMessageRecorder | None = None) -> None:
    cfg = load(config_path)
    state = ProtocolState(network_id=b"\xff\xff\xff\xff", association_id=0xFF)
    async with SerialTransport(cfg.serial.port, cfg.serial.speed, raw_recorder=raw_recorder) as transport:
        await transport.listen()
        client = FrisquetClient(transport, state)
        print(f"Waiting for boiler association broadcast for {role} — trigger pairing on the boiler...")
        assoc = await client.pair(PAIR_ROLES[role])
    cfg.network_id = assoc.network_id
    cfg.set_identity(role, association_id=assoc.association_id, request_id=assoc.request_id)
    cfg.save()
    print(f"Paired {role}: network_id={assoc.network_id.hex()} association_id={assoc.association_id:#04x}")
