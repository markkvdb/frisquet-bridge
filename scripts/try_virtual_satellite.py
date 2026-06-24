"""One-off experiment: act as the (virtual) zone satellite and check that the
boiler accepts our consigne.

Unlike a Connect zone write (which the boiler just relays to the sleeping
satellite), a satellite consigne is a real request/response: the satellite sends
its ambient + setpoint + mode at 0xa02f and the boiler answers directly with a
0x81 ACK (the 0x2a01 boiler-state block). If that ACK comes back, the boiler is
treating us as the authoritative satellite for the zone -- which is the whole
point of going virtual.

This sends a few consignes as address 0x08 (zone 1) and reports each boiler ACK.

CAUTION: the physical satellite is still on 0x08 with the same association, so
keep this test SHORT to avoid both devices talking at once. Run with `serve`
STOPPED. Example:

    uv run python scripts/try_virtual_satellite.py --setpoint 19 --ambient 20
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime

from frisquet_bridge.config import load
from frisquet_bridge.connect.client import FrisquetClient
from frisquet_bridge.connect.ops import BoilerOps
from frisquet_bridge.connect.state import ProtocolState
from frisquet_bridge.emulation import PassiveMirror
from frisquet_bridge.logging import RawMessageRecorder
from frisquet_bridge.model import BoilerData, ZoneMode
from frisquet_bridge.state_store import load_zone_state
from frisquet_bridge.transport.base import TransportError
from frisquet_bridge.transport.serial import SerialTransport

_ZONE_SAT_ADDR = {1: 0x08, 2: 0x09, 3: 0x0A}
_MODES = {m.name.lower(): m for m in ZoneMode}


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--zone", type=int, default=1)
    parser.add_argument("--ambient", type=float, default=20.0, help="room temperature to report")
    parser.add_argument("--setpoint", type=float, default=19.0, help="active setpoint to request")
    parser.add_argument("--mode", default="auto", choices=sorted(_MODES))
    parser.add_argument("--assoc", default="c0", help="satellite association id (hex)")
    parser.add_argument("--count", type=int, default=3, help="number of consignes to send")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--raw-log-file", default="logs/try-vsat.raw.jsonl")
    args = parser.parse_args()

    cfg = load(args.config)
    zone = args.zone
    sat_addr = _ZONE_SAT_ADDR[zone]

    data = BoilerData()
    load_zone_state(cfg.state_path, data)
    zs = data.zones[zone]
    zs.mode = _MODES[args.mode]
    zs.override = False
    zs.auto_comfort = False if zs.mode == ZoneMode.AUTO else None

    recorder = RawMessageRecorder(args.raw_log_file, max_bytes=50_000_000, backup_count=2)
    transport = SerialTransport(cfg.serial.port, cfg.serial.speed, raw_recorder=recorder)
    sat_state = ProtocolState(network_id=cfg.network_id, association_id=int(args.assoc, 16))
    client = FrisquetClient(transport, sat_state, self_addr=sat_addr, boiler_addr=cfg.boiler_addr)
    ops = BoilerOps(client, boiler_addr=cfg.boiler_addr, memory_offset=cfg.memory_offset)
    mirror = PassiveMirror(data, boiler_addr=cfg.boiler_addr)

    stop = asyncio.Event()

    async def sniff() -> None:
        async for received in transport.frames():
            await mirror.handle(received)
            print(f"[{_now()}] RSSI {received.rssi:4d}  {received.frame.describe()}")
            if stop.is_set():
                break

    async with transport:
        await transport.listen()
        sniff_task = asyncio.create_task(sniff())
        acked = 0
        try:
            print(
                f"[{_now()}] acting as satellite 0x{sat_addr:02x} (assoc 0x{sat_state.association_id:02x}); "
                f"mode={args.mode} setpoint={args.setpoint} ambient={args.ambient}"
            )
            for i in range(1, args.count + 1):
                try:
                    await ops.send_zone_consigne(
                        zone, data, ambient=args.ambient, setpoint=args.setpoint
                    )
                    acked += 1
                    b = data.boiler
                    print(
                        f"[{_now()}] consigne #{i}: boiler ACKed "
                        f"(outside={b.outside_temperature} status={b.status})"
                    )
                except TransportError as exc:
                    print(f"[{_now()}] consigne #{i}: NO ACK ({exc})")
                if i < args.count:
                    await asyncio.sleep(args.interval)
        finally:
            stop.set()
            sniff_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sniff_task
            recorder.close()

        if acked:
            print(
                f"[{_now()}] RESULT: boiler accepted {acked}/{args.count} consignes -> "
                "it treats us as the satellite. Virtual satellite is viable."
            )
        else:
            print(f"[{_now()}] RESULT: boiler never ACKed; check association/network id.")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
