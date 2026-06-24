"""RF sniffer - listen for Frisquet boiler / Connect traffic."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from frisquet_bridge.cli.options import add_logging_options
from frisquet_bridge.config import load
from frisquet_bridge.connect.passive import PassiveReadTracker
from frisquet_bridge.frame import ADDR_BOILER
from frisquet_bridge.logging import RawMessageRecorder
from frisquet_bridge.protocol import default_serial_port
from frisquet_bridge.transport.serial import SerialTransport


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "listen",
        help="Sniff RF traffic via the Feather M0 modem",
        description="Listen for Frisquet RF frames (boiler, Connect box, satellites).",
    )
    p.add_argument("--config", default="config.toml", help="Config file path for serial/network/boiler defaults")
    p.add_argument("--port", default=default_serial_port(), help="Serial port")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument(
        "--network-id",
        help="4-byte sync word as hex (default: config network_id when config exists, otherwise ffffffff)",
    )
    p.add_argument(
        "--boiler-addr",
        choices=("80", "84"),
        help="Boiler RF address for passive decoding (default: config boiler_addr, or 80)",
    )
    p.add_argument(
        "--promiscuous",
        action="store_true",
        help="Use sync word ffffffff for association/pairing traffic; this is not a wildcard for all networks",
    )
    add_logging_options(p, suppress_default=True)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    port = args.port
    baud = args.baud
    boiler_addr = ADDR_BOILER
    network_id = args.network_id

    config_path = Path(args.config)
    if config_path.exists():
        cfg = load(args.config)
        port = cfg.serial.port if args.port == default_serial_port() else args.port
        baud = cfg.serial.speed if args.baud == 115200 else args.baud
        boiler_addr = cfg.boiler_addr
        if network_id is None:
            network_id = cfg.network_id.hex()

    if args.boiler_addr is not None:
        boiler_addr = int(args.boiler_addr, 16)

    network_id = "ffffffff" if args.promiscuous else (network_id or "ffffffff").replace(" ", "")
    if len(network_id) != 8:
        print("network-id must be 8 hex chars (4 bytes)")
        return 1
    try:
        asyncio.run(_listen(port, baud, network_id, boiler_addr, args.raw_recorder))
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


async def _listen(port: str, baud: int, network_id: str, boiler_addr: int, raw_recorder: RawMessageRecorder | None = None) -> None:
    print(f"Opening {port} @ {baud}...")
    print(f"Sync word (NID): {network_id}")
    if network_id.lower() == "ffffffff":
        print("Note: ffffffff is a real sync word, not a wildcard for every network.")
    print(f"Boiler address: 0x{boiler_addr:02x}")
    print("Press Ctrl+C to stop.\n")

    read_tracker = PassiveReadTracker(boiler_addr=boiler_addr)

    async with SerialTransport(port, baud, raw_recorder=raw_recorder) as transport:
        await transport.set_network_id(bytes.fromhex(network_id))
        await transport.listen()
        async for received in transport.frames():
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"[{ts}] RSSI {received.rssi:4d} dBm  {received.frame.describe()}")
            print(f"         raw={received.raw.hex()}")
            decoded = read_tracker.describe(received.frame)
            if decoded:
                print(f"         {decoded}")
