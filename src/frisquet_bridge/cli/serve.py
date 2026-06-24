"""Long-running bridge service."""

from __future__ import annotations

import argparse
import asyncio
import signal

from frisquet_bridge.cli.options import add_logging_options
from frisquet_bridge.config import BridgeConfig, load
from frisquet_bridge.logging import RawMessageRecorder
from frisquet_bridge.service import BridgeService


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("serve", help="Run the bridge service (polling + optional MQTT)")
    p.add_argument("--config", default="config.toml", help="Config file path")
    add_logging_options(p, suppress_default=True)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    cfg = load(args.config)
    try:
        asyncio.run(_serve(cfg, args.raw_recorder))
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


async def _serve(cfg: BridgeConfig, raw_recorder: RawMessageRecorder | None) -> None:
    service = BridgeService(cfg, raw_recorder=raw_recorder)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, service.stop)
    await service.run()
