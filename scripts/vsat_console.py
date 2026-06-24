"""Interactive virtual-satellite console for exercising zone functionality.

Acts as the zone's satellite (address 0x08 / assoc 0xc0) and lets you drive every
mode, derogation, setpoint and schedule by hand, watching the boiler's response.
Mode/preset/target commands go through the SAME climate logic the HA/MQTT path
uses (resolve_zone_intent + apply_zone_intent), so this is a faithful end-to-end
test of the virtual-satellite control surface.

Two transmit paths are available:
  * consigne (0xa02f)  -- the virtual-satellite report; reliable request/ACK.
                          Sent automatically after every mode/setpoint change.
  * push    (0xa154)   -- a Connect config write carrying the full setpoints +
                          weekly schedule (the only way to (re)program a schedule).

Run with `serve` STOPPED. The physical satellite is still on 0x08, so keep
sessions reasonably short. Example:

    uv run python scripts/vsat_console.py

Commands (type `help` in the console for the full list):
    auto | auto comfort | auto eco | comfort | eco | frost | boost
    set comfort|eco|frost|ambient|target <value>
    sched [<day> <hex12>] | push | send | status | help | quit
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime

from frisquet_bridge.climate import (
    HVAC_AUTO,
    HVAC_HEAT,
    HVAC_OFF,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
    apply_zone_intent,
    resolve_zone_intent,
)
from frisquet_bridge.config import load
from frisquet_bridge.connect.client import FrisquetClient
from frisquet_bridge.connect.ops import BoilerOps
from frisquet_bridge.connect.state import ProtocolState
from frisquet_bridge.emulation import PassiveMirror
from frisquet_bridge.logging import RawMessageRecorder
from frisquet_bridge.model import SCHEDULE_DAYS, BoilerData, ZoneMode, ZoneSchedule, ZoneState
from frisquet_bridge.state_store import load_zone_state
from frisquet_bridge.transport.base import TransportError
from frisquet_bridge.transport.serial import SerialTransport

_ZONE_SAT_ADDR = {1: 0x08, 2: 0x09, 3: 0x0A}
_DEFAULTS = {"comfort": 20.0, "eco": 17.0, "frost": 7.0, "ambient": 20.0}

# command -> (hvac_mode, preset)
_MODE_CMDS: dict[str, tuple[str, str]] = {
    "auto": (HVAC_AUTO, PRESET_NONE),
    "auto comfort": (HVAC_AUTO, PRESET_COMFORT),
    "auto eco": (HVAC_AUTO, PRESET_ECO),
    "comfort": (HVAC_HEAT, PRESET_COMFORT),
    "eco": (HVAC_HEAT, PRESET_ECO),
    "reduced": (HVAC_HEAT, PRESET_ECO),
    "frost": (HVAC_OFF, PRESET_NONE),
    "off": (HVAC_OFF, PRESET_NONE),
    "boost": (HVAC_HEAT, PRESET_BOOST),
}

HELP = """\
modes:
  auto                follow the program (clears any derogation)
  auto comfort        auto + comfort derogation (until next program change)
  auto eco            auto + eco derogation
  comfort             permanent comfort
  eco | reduced       permanent reduced
  frost | off         frost protection (heating off)
  boost               comfort + boost (+2C)
setpoints (auto-resends a consigne):
  set comfort <T>     comfort temperature
  set eco <T>         reduced temperature
  set frost <T>       frost temperature
  set ambient <T>     room temperature we report to the boiler
  set target <T>      target for the current mode (like the HA slider)
schedule:
  sched               show the weekly schedule
  sched <day> <hex>   set a day's 6-byte slot map, e.g. `sched monday ffffffffffff`
  push                push setpoints + schedule via a Connect write (0xa154)
control:
  send                re-send the consigne now
  status              show current state and resolved setpoint
  help                this help
  quit                exit
"""


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _active_setpoint(zs: ZoneState) -> float | None:
    """The setpoint a satellite would report for the current mode.

    In auto we follow the comfort/eco flag (as OpenFrisquetVisio's confortActif
    does) rather than the boiler's echoed setpoint, so the value is deterministic
    for testing.
    """
    if zs.mode == ZoneMode.FROST:
        return zs.frost_temperature
    if zs.mode == ZoneMode.REDUCED:
        return zs.reduced_temperature
    if zs.mode == ZoneMode.COMFORT:
        base = zs.comfort_temperature
        return base + 2.0 if (zs.boost and base is not None) else base
    if zs.mode == ZoneMode.AUTO:
        return zs.reduced_temperature if zs.auto_comfort is False else zs.comfort_temperature
    return None


def _ambient(zs: ZoneState) -> float:
    if zs.reported_ambient is not None:
        return zs.reported_ambient
    if zs.ambient_temperature is not None:
        return zs.ambient_temperature
    return _DEFAULTS["ambient"]


def _summary(zs: ZoneState) -> str:
    mode = zs.mode.value if zs.mode else "?"
    deg = " override" if zs.override else ""
    ac = {True: " comfort", False: " eco", None: ""}[zs.auto_comfort]
    return (
        f"mode={mode}{deg}{ac} boost={zs.boost} | active_setpoint={_active_setpoint(zs)} "
        f"ambient={_ambient(zs)} | comfort={zs.comfort_temperature} eco={zs.reduced_temperature} "
        f"frost={zs.frost_temperature}"
    )


class Console:
    def __init__(self, cfg, zone: int, sat_assoc: int, verbose: bool) -> None:
        self.cfg = cfg
        self.zone = zone
        self.verbose = verbose
        self.data = BoilerData()
        load_zone_state(cfg.state_path, self.data)
        zs = self.data.zones[zone]
        zs.comfort_temperature = zs.comfort_temperature or _DEFAULTS["comfort"]
        zs.reduced_temperature = zs.reduced_temperature or _DEFAULTS["eco"]
        zs.frost_temperature = zs.frost_temperature or _DEFAULTS["frost"]
        zs.reported_ambient = _ambient(zs)
        if zs.mode is None:
            zs.mode = ZoneMode.AUTO

        self.recorder = RawMessageRecorder("logs/vsat-console.raw.jsonl", max_bytes=50_000_000, backup_count=2)
        self.transport = SerialTransport(cfg.serial.port, cfg.serial.speed, raw_recorder=self.recorder)
        lock = asyncio.Lock()
        sat_state = ProtocolState(network_id=cfg.network_id, association_id=sat_assoc)
        sat_client = FrisquetClient(
            self.transport, sat_state, self_addr=_ZONE_SAT_ADDR[zone], boiler_addr=cfg.boiler_addr, lock=lock
        )
        self.sat_ops = BoilerOps(sat_client, boiler_addr=cfg.boiler_addr, memory_offset=cfg.memory_offset)
        self.connect_ops: BoilerOps | None = None
        if cfg.connect is not None:
            connect_state = ProtocolState(**cfg.protocol_state_kwargs("connect"))
            connect_client = FrisquetClient(self.transport, connect_state, boiler_addr=cfg.boiler_addr, lock=lock)
            self.connect_ops = BoilerOps(connect_client, boiler_addr=cfg.boiler_addr, memory_offset=cfg.memory_offset)
        self.mirror = PassiveMirror(self.data, boiler_addr=cfg.boiler_addr)

    async def sniff(self, stop: asyncio.Event) -> None:
        async for received in self.transport.frames():
            await self.mirror.handle(received)
            if self.verbose:
                print(f"\n[{_now()}] RSSI {received.rssi:4d}  {received.frame.describe()}")
            if stop.is_set():
                break

    async def send_consigne(self) -> None:
        zs = self.data.zones[self.zone]
        setpoint = _active_setpoint(zs)
        if setpoint is None:
            print("  cannot send: active setpoint unknown")
            return
        try:
            await self.sat_ops.send_zone_consigne(self.zone, self.data, ambient=_ambient(zs), setpoint=setpoint)
            b = self.data.boiler
            print(f"  consigne sent: setpoint={setpoint} ambient={_ambient(zs)} | boiler ACK ok "
                  f"(outside={b.outside_temperature} status={b.status})")
        except TransportError as exc:
            print(f"  consigne NOT ACKed: {exc}")

    async def apply_intent(self, hvac: str, preset: str) -> None:
        zs = self.data.zones[self.zone]
        intent = resolve_zone_intent(zs, hvac_mode=hvac, preset=preset)
        apply_zone_intent(zs, intent)
        print(f"  -> {_summary(zs)}")
        await self.send_consigne()

    async def set_value(self, what: str, value: float) -> None:
        zs = self.data.zones[self.zone]
        if what == "comfort":
            zs.comfort_temperature = value
        elif what in {"eco", "reduced"}:
            zs.reduced_temperature = value
        elif what == "frost":
            zs.frost_temperature = value
        elif what == "ambient":
            zs.reported_ambient = value
        elif what == "target":
            intent = resolve_zone_intent(zs, target_temperature=value)
            apply_zone_intent(zs, intent)
        else:
            print(f"  unknown setpoint '{what}'")
            return
        print(f"  -> {_summary(zs)}")
        await self.send_consigne()

    def show_schedule(self) -> None:
        zs = self.data.zones[self.zone]
        if zs.schedule is None:
            print("  no schedule learned/loaded yet (use `sched <day> <hex>` to set one)")
            return
        for day in SCHEDULE_DAYS:
            print(f"  {day:<9} {zs.schedule.days[day].hex()}")

    def set_schedule_day(self, day: str, hex_value: str) -> None:
        zs = self.data.zones[self.zone]
        day = day.strip().casefold()
        if day not in SCHEDULE_DAYS:
            print(f"  unknown day '{day}'; expected one of {', '.join(SCHEDULE_DAYS)}")
            return
        try:
            raw = bytes.fromhex(hex_value)
        except ValueError:
            print("  invalid hex")
            return
        if len(raw) != 6:
            print(f"  a day needs exactly 6 bytes (12 hex chars), got {len(raw)}")
            return
        days = dict(zs.schedule.days) if zs.schedule else {d: b"\xff" * 6 for d in SCHEDULE_DAYS}
        days[day] = raw
        zs.schedule = ZoneSchedule(days=days)
        print(f"  {day} -> {raw.hex()} (use `push` to program it into the zone)")

    async def push(self) -> None:
        if self.connect_ops is None:
            print("  no Connect identity configured; cannot push a schedule write")
            return
        zs = self.data.zones[self.zone]
        try:
            await self.connect_ops.write_zone_short(self.zone, self.data, mode=zs.mode)
            print("  pushed config+schedule via Connect write (fire-and-forget burst; "
                  "lands only when the physical satellite is awake)")
        except Exception as exc:  # noqa: BLE001 - surface any failure to the console
            print(f"  push failed: {exc}")

    async def handle(self, line: str) -> bool:
        line = line.strip()
        if not line:
            return True
        parts = line.split()
        cmd = parts[0].casefold()
        two = " ".join(parts[:2]).casefold()

        if cmd in {"quit", "exit", "q"}:
            return False
        if cmd == "help":
            print(HELP)
        elif cmd == "status":
            print(f"  {_summary(self.data.zones[self.zone])}")
        elif cmd == "send":
            await self.send_consigne()
        elif cmd == "push":
            await self.push()
        elif cmd == "sched":
            if len(parts) >= 3:
                self.set_schedule_day(parts[1], parts[2])
            else:
                self.show_schedule()
        elif two in _MODE_CMDS:
            await self.apply_intent(*_MODE_CMDS[two])
        elif cmd in _MODE_CMDS:
            await self.apply_intent(*_MODE_CMDS[cmd])
        elif cmd == "set" and len(parts) >= 3:
            try:
                await self.set_value(parts[1].casefold(), float(parts[2]))
            except ValueError:
                print("  usage: set comfort|eco|frost|ambient|target <number>")
        else:
            print(f"  unknown command '{line}'. type `help`.")
        return True

    async def run(self) -> None:
        stop = asyncio.Event()
        async with self.transport:
            await self.transport.listen()
            sniff_task = asyncio.create_task(self.sniff(stop))
            loop = asyncio.get_running_loop()
            print(f"virtual-satellite console: zone {self.zone}. type `help`, `quit` to exit.")
            print(f"  {_summary(self.data.zones[self.zone])}")
            try:
                while True:
                    line = await loop.run_in_executor(None, input, "vsat> ")
                    if not await self.handle(line):
                        break
            except (EOFError, KeyboardInterrupt):
                pass
            finally:
                stop.set()
                sniff_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sniff_task
                self.recorder.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--zone", type=int, default=1)
    parser.add_argument("--assoc", default="c0", help="satellite association id (hex)")
    parser.add_argument("--verbose", action="store_true", help="print every sniffed frame")
    args = parser.parse_args()
    cfg = load(args.config)
    await Console(cfg, args.zone, int(args.assoc, 16), args.verbose).run()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
