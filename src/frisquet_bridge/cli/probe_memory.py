"""Read arbitrary boiler memory blocks for reverse-engineering."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from frisquet_bridge.cli.options import add_logging_options
from frisquet_bridge.config import BridgeConfig, ConfigError, load
from frisquet_bridge.connect.client import FrisquetClient
from frisquet_bridge.connect.decode import (
    ADDR_CONSUMPTION,
    ADDR_DAILY_CONSUMPTION,
    ADDR_SENSORS,
    ADDR_SENSORS_APP,
    decode_consumption,
    decode_daily_consumption,
    decode_sensors,
)
from frisquet_bridge.connect.state import ProtocolState
from frisquet_bridge.logging import RawMessageRecorder
from frisquet_bridge.model import BoilerData
from frisquet_bridge.transport.base import TransportError
from frisquet_bridge.transport.serial import SerialTransport

DEFAULT_ADDRESSES = (0x79C4, 0x79E0, 0x79FC, 0x7A18, 0x7A34, 0x7A50, 0x7A6C)
DEFAULT_SIZE = 0x001C


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "probe-memory",
        help="Read boiler memory blocks repeatedly for reverse-engineering",
        description=(
            "Read one or more boiler memory blocks with the paired Connect identity. "
            "Address specs may be single values like 0x7a18 or ranges like 0x79c4-0x7a6c/0x1c."
        ),
    )
    p.add_argument("addresses", nargs="*", help="Memory address specs; defaults to the known sensor/consumption neighborhood")
    p.add_argument("--config", default="config.toml", help="Config file path")
    p.add_argument("--size", default=f"0x{DEFAULT_SIZE:04x}", help="READ size for every block, default 0x001c")
    p.add_argument("--step", default="0x001c", help="Default range step when not specified with /step")
    p.add_argument("--count", type=int, default=1, help="Number of full probe passes, default 1")
    p.add_argument("--interval", type=float, default=30.0, help="Seconds between probe passes when count > 1")
    p.add_argument("--delay", type=float, default=0.25, help="Seconds between addresses inside one pass")
    p.add_argument("--timeout", type=float, default=5.0, help="Per-read response timeout")
    p.add_argument("--retries", type=int, default=2, help="Per-read retry count")
    p.add_argument(
        "--absolute",
        action="store_true",
        help="Do not apply the boiler 0x84 memory offset; treat addresses as exact wire addresses",
    )
    p.add_argument("--jsonl", help="Optional JSONL file for decoded probe rows")
    add_logging_options(p, suppress_default=True)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    try:
        asyncio.run(_probe(args, args.raw_recorder))
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


async def _probe(args: argparse.Namespace, raw_recorder: RawMessageRecorder | None = None) -> None:
    cfg = load(args.config)
    _require_connect_reader(cfg)
    size = _parse_int(args.size)
    step = _parse_int(args.step)
    addresses = parse_address_specs(args.addresses, default_step=step)
    if not addresses:
        addresses = list(DEFAULT_ADDRESSES)
    if size <= 0 or size > 0x00FF:
        raise ConfigError("probe-memory --size must be between 1 and 255")
    if args.count <= 0:
        raise ConfigError("probe-memory --count must be positive")
    if args.interval < 0 or args.delay < 0:
        raise ConfigError("probe-memory --interval and --delay must be non-negative")
    if args.retries <= 0:
        raise ConfigError("probe-memory --retries must be positive")

    state = ProtocolState(**cfg.protocol_state_kwargs("connect"))
    offset = 0 if args.absolute else cfg.memory_offset
    jsonl_path = Path(args.jsonl) if args.jsonl else None
    if jsonl_path is not None:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Probing {len(addresses)} address(es), size=0x{size:04x}, count={args.count}")
    if offset:
        print(f"Applying boiler memory offset +0x{offset:04x}; use --absolute to disable.")
    print("Press Ctrl+C to stop.\n")

    baselines: dict[int, bytes] = {}
    async with SerialTransport(cfg.serial.port, cfg.serial.speed, raw_recorder=raw_recorder) as transport:
        await transport.listen()
        client = FrisquetClient(transport, state, boiler_addr=cfg.boiler_addr)
        with _jsonl_writer(jsonl_path) as writer:
            for sample in range(args.count):
                if sample and args.interval:
                    await asyncio.sleep(args.interval)
                print(f"sample={sample + 1}/{args.count} ts={_utc_now()}")
                for index, base_addr in enumerate(addresses):
                    actual_addr = base_addr + offset
                    row = await _read_probe_row(
                        client,
                        sample=sample,
                        base_addr=base_addr,
                        actual_addr=actual_addr,
                        size=size,
                        timeout=args.timeout,
                        retries=args.retries,
                        baselines=baselines,
                    )
                    print(_format_row(row))
                    if writer is not None:
                        writer.write(json.dumps(row, sort_keys=True) + "\n")
                        writer.flush()
                    if index + 1 < len(addresses) and args.delay:
                        await asyncio.sleep(args.delay)
                print()


async def _read_probe_row(
    client: FrisquetClient,
    *,
    sample: int,
    base_addr: int,
    actual_addr: int,
    size: int,
    timeout: float,
    retries: int,
    baselines: dict[int, bytes],
) -> dict[str, Any]:
    ts = _utc_now()
    row: dict[str, Any] = {
        "ts": ts,
        "sample": sample,
        "address": f"0x{base_addr:04x}",
        "actual_address": f"0x{actual_addr:04x}",
        "size": size,
        "name": _known_name(base_addr),
    }
    try:
        payload = await client.read_memory(actual_addr, size, timeout=timeout, retries=retries)
    except TransportError as exc:
        row["error"] = str(exc)
        return row

    baseline = baselines.setdefault(base_addr, payload)
    row["payload_len"] = len(payload)
    row["payload_hex"] = payload.hex()
    row["changed_offsets"] = [
        {"offset": offset, "baseline": _hex_or_none(old), "value": _hex_or_none(new)}
        for offset, old, new in changed_offsets(baseline, payload)
    ]
    summary = _decode_summary(base_addr, payload)
    if summary:
        row["summary"] = summary
    return row


def parse_address_specs(specs: list[str], *, default_step: int) -> list[int]:
    if default_step <= 0:
        raise ConfigError("probe-memory --step must be positive")
    addresses: list[int] = []
    for spec in specs:
        normalized = spec.strip().lower().replace(":", "-")
        if not normalized:
            continue
        if "/" in normalized:
            span, step_text = normalized.split("/", 1)
            step = _parse_int(step_text)
            if step <= 0:
                raise ConfigError(f"invalid address spec {spec!r}: step must be positive")
        else:
            span = normalized
            step = default_step

        if "-" not in span:
            addresses.append(_parse_address(span, spec))
            continue

        start_text, end_text = span.split("-", 1)
        start = _parse_address(start_text, spec)
        end = _parse_address(end_text, spec)
        if end < start:
            raise ConfigError(f"invalid address spec {spec!r}: range end is before start")
        addresses.extend(range(start, end + 1, step))

    return sorted(dict.fromkeys(addresses))


def changed_offsets(baseline: bytes, payload: bytes) -> list[tuple[int, int | None, int | None]]:
    changes: list[tuple[int, int | None, int | None]] = []
    for offset in range(max(len(baseline), len(payload))):
        old = baseline[offset] if offset < len(baseline) else None
        new = payload[offset] if offset < len(payload) else None
        if old != new:
            changes.append((offset, old, new))
    return changes


def _require_connect_reader(cfg: BridgeConfig) -> None:
    if not cfg.connect_reads_enabled:
        raise ConfigError('probe-memory requires [frisquet.connect] mode = "read" or "full" with a paired identity')


def _parse_address(text: str, original_spec: str) -> int:
    value = _parse_int(text)
    if value < 0 or value > 0xFFFF:
        raise ConfigError(f"invalid address spec {original_spec!r}: address must be between 0x0000 and 0xffff")
    return value


def _parse_int(text: str) -> int:
    try:
        return int(text.strip(), 0)
    except ValueError as exc:
        raise ConfigError(f"invalid integer: {text!r}") from exc


def _format_row(row: dict[str, Any]) -> str:
    prefix = f"  {row['address']}"
    if row["actual_address"] != row["address"]:
        prefix += f" actual={row['actual_address']}"
    if row.get("name"):
        prefix += f" {row['name']}"
    if row.get("error"):
        return f"{prefix} ERROR {row['error']}"

    summary = row.get("summary")
    diff = _format_changes(row.get("changed_offsets", []))
    text = f"{prefix} len={row['payload_len']} payload={row['payload_hex']}"
    if summary:
        text += f" {summary}"
    if diff:
        text += f" diff={diff}"
    return text


def _format_changes(changes: list[dict[str, str | int | None]]) -> str:
    if not changes:
        return ""
    parts = []
    for change in changes[:24]:
        parts.append(f"{change['offset']}:{change['baseline']}->{change['value']}")
    if len(changes) > 24:
        parts.append(f"...+{len(changes) - 24}")
    return ",".join(parts)


def _decode_summary(base_addr: int, payload: bytes) -> str:
    data = BoilerData()
    try:
        if base_addr in (ADDR_SENSORS, ADDR_SENSORS_APP):
            decode_sensors(payload, data)
            b = data.boiler
            return f"dhw_power={_fmt(b.dhw_power)}kW heating_power={_fmt(b.heating_power)}kW pressure={_fmt(b.pressure)}bar"
        if base_addr == ADDR_DAILY_CONSUMPTION:
            decode_daily_consumption(payload, data)
            b = data.boiler
            return f"daily_dhw={b.daily_dhw_consumption}kWh daily_heating={b.daily_heating_consumption}kWh"
        if base_addr == ADDR_CONSUMPTION:
            decode_consumption(payload, data)
            b = data.boiler
            return f"dhw={b.dhw_consumption}kWh heating={b.heating_consumption}kWh"
    except ValueError as exc:
        return f"decode_error={exc}"
    return ""


def _known_name(base_addr: int) -> str:
    return {
        ADDR_SENSORS: "sensors",
        ADDR_SENSORS_APP: "sensors_app",
        ADDR_DAILY_CONSUMPTION: "daily_consumption",
        ADDR_CONSUMPTION: "consumption",
    }.get(base_addr, "")


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def _hex_or_none(value: int | None) -> str | None:
    return None if value is None else f"0x{value:02x}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class _jsonl_writer:
    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._handle: Any = None

    def __enter__(self) -> Any:
        if self._path is None:
            return None
        self._handle = self._path.open("a", encoding="utf-8")
        return self._handle

    def __exit__(self, *_args: object) -> None:
        if self._handle is not None:
            self._handle.close()
