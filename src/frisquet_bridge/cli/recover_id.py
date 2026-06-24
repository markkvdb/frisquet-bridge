"""Recover the boiler's network id by sniffing an association broadcast."""

from __future__ import annotations

import argparse
import asyncio

from frisquet_bridge.cli.options import add_logging_options
from frisquet_bridge.config import load
from frisquet_bridge.connect.client import FrisquetClient
from frisquet_bridge.connect.state import ProtocolState
from frisquet_bridge.logging import RawMessageRecorder
from frisquet_bridge.transport.serial import SerialTransport


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "recover-id",
        help="Recover the boiler network id (sniff association, no reply)",
    )
    p.add_argument("--config", default="config.toml", help="Config file path")
    p.add_argument("--timeout", type=float, default=30.0, help="Seconds to wait for a broadcast")
    p.add_argument("--save", action="store_true", help="Persist the recovered network id to the config")
    add_logging_options(p, suppress_default=True)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    try:
        asyncio.run(_recover(args.config, args.timeout, args.save, args.raw_recorder))
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


async def _recover(
    config_path: str,
    timeout: float,
    save: bool,
    raw_recorder: RawMessageRecorder | None = None,
) -> None:
    cfg = load(config_path)
    state = ProtocolState(network_id=b"\xff\xff\xff\xff", association_id=0xFF)
    async with SerialTransport(cfg.serial.port, cfg.serial.speed, raw_recorder=raw_recorder) as transport:
        await transport.listen()
        client = FrisquetClient(transport, state, boiler_addr=cfg.boiler_addr)
        print("Trigger an association (or 'replace satellite') on the boiler — waiting for the broadcast...")
        network_id = await client.recover_network_id(timeout=timeout)
    print(f"Recovered network_id={network_id.hex()}")
    if save:
        cfg.network_id = network_id
        cfg.save()
        print(f"Saved to {config_path}. You can now cancel the association on the boiler.")
    else:
        print("Re-run with --save to persist it. You can now cancel the association on the boiler.")
