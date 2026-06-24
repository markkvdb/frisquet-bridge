"""One-off experiment: push a zone AUTO write from the (emulated) Connect and
watch whether the physical satellite actually switches the zone to auto.

The boiler does NOT persist a zone write: it just relays the Connect's 0xa154
frame to the satellite over RF. The write only "lands" if the satellite happens
to be awake to hear that relay, at which point it replies with a control=0xfe
ACK and adopts the config. The satellite only wakes periodically (minutes
apart), so the official app re-sends the write over and over until one relay
coincides with a wake window. We do the same here: keep re-sending until we see
the satellite's 0xfe ACK (or an 0xa02f check-in whose setpoint leaves frost).

Run with `serve` STOPPED (it would fight for the serial port and also transmit
as Connect/sonde). Example:

    uv run python scripts/try_zone_auto.py --minutes 8
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
from frisquet_bridge.frame import MSG_INIT
from frisquet_bridge.logging import RawMessageRecorder
from frisquet_bridge.model import BoilerData, ZoneMode
from frisquet_bridge.state_store import load_zone_state
from frisquet_bridge.transport.serial import SerialTransport

_ZONE_SAT_ADDR = {1: 0x08, 2: 0x09, 3: 0x0A}


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _summary(data: BoilerData, zone: int) -> str:
    zs = data.zones[zone]
    mode = zs.mode.value if zs.mode is not None else "?"
    return f"mode={mode} setpoint={zs.setpoint_temperature} ambient={zs.ambient_temperature} override={zs.override}"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--zone", type=int, default=1)
    parser.add_argument("--minutes", type=float, default=8.0)
    parser.add_argument("--raw-log-file", default="logs/try-auto.raw.jsonl")
    args = parser.parse_args()

    cfg = load(args.config)
    data = BoilerData()
    load_zone_state(cfg.state_path, data)

    zone = args.zone
    sat_addr = _ZONE_SAT_ADDR[zone]
    frost = data.zones[zone].frost_temperature or 8.0

    recorder = RawMessageRecorder(args.raw_log_file, max_bytes=50_000_000, backup_count=2)
    transport = SerialTransport(cfg.serial.port, cfg.serial.speed, raw_recorder=recorder)
    state = ProtocolState(**cfg.protocol_state_kwargs("connect"))
    client = FrisquetClient(transport, state, boiler_addr=cfg.boiler_addr)
    ops = BoilerOps(client, boiler_addr=cfg.boiler_addr, memory_offset=cfg.memory_offset)
    mirror = PassiveMirror(data, boiler_addr=cfg.boiler_addr)

    print(f"[{_now()}] zone {zone} before write: {_summary(data, zone)}  (frost setpoint = {frost})")

    stop = asyncio.Event()
    confirmed = asyncio.Event()

    async def sniff() -> None:
        async for received in transport.frames():
            await mirror.handle(received)
            frame = received.frame
            print(f"[{_now()}] RSSI {received.rssi:4d}  {frame.describe()}")
            # Strongest proof: the satellite ACKs the relayed write (control 0xfe)
            # echoing our config with mode byte 0x05 (auto).
            sat_acked = (
                frame.from_addr == sat_addr
                and frame.control == 0xFE
                and len(frame.payload) >= 5
                and frame.payload[4] == 0x05
            )
            is_sat_report = (
                frame.from_addr == sat_addr and frame.to_addr == cfg.boiler_addr and frame.msg_type == MSG_INIT
            )
            if sat_acked:
                print(f"[{_now()}]   -> satellite ACKed the AUTO write (0xfe)")
                confirmed.set()
            elif is_sat_report:
                zs = data.zones[zone]
                print(f"[{_now()}]   -> satellite report: {_summary(data, zone)}")
                left_frost = zs.setpoint_temperature is not None and zs.setpoint_temperature >= frost + 1.0
                if zs.mode == ZoneMode.AUTO or left_frost:
                    confirmed.set()
            if stop.is_set() or confirmed.is_set():
                break

    async def resend() -> None:
        # The satellite wakes only every few minutes; keep relaying the write so
        # one transmission lands inside a wake window (this is what the app does).
        attempt = 0
        while not confirmed.is_set() and not stop.is_set():
            attempt += 1
            print(f"[{_now()}] AUTO write attempt #{attempt} (burst)...")
            await ops.write_zone_short(zone, data, mode=ZoneMode.AUTO)
            for _ in range(20):  # ~2s pause between bursts, but bail out fast
                if confirmed.is_set() or stop.is_set():
                    return
                await asyncio.sleep(0.1)

    async with transport:
        await transport.listen()
        sniff_task = asyncio.create_task(sniff())
        resend_task = asyncio.create_task(resend())
        try:
            print(f"[{_now()}] re-sending AUTO write until confirmed (up to {args.minutes} min)")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(confirmed.wait(), timeout=args.minutes * 60)
            if confirmed.is_set():
                print(f"[{_now()}] CONFIRMED: satellite accepted AUTO for zone {zone} -> {_summary(data, zone)}")
            else:
                print(f"[{_now()}] NOT confirmed within {args.minutes} min; zone still {_summary(data, zone)}")
        finally:
            stop.set()
            for task in (resend_task, sniff_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            recorder.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
