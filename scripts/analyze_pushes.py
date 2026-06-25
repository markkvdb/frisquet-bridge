#!/usr/bin/env python3
"""Offline diff tool for boiler-initiated RF pushes (control=0x05).

Groups frames by message type and (for 0x10) memory-block address, then prints
an aligned per-byte matrix highlighting constant vs varying columns.

Example:
    uv run python scripts/analyze_pushes.py logs/*.raw.jsonl
    uv run python scripts/analyze_pushes.py logs/playing-with-boiler.raw.jsonl --stamp-delta
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PushFrame:
    source: str
    ts: str
    msg_type: int
    request_id: int
    payload: bytes
    rssi: int | None

    @property
    def group_key(self) -> tuple[int, str]:
        if self.msg_type == 0x10 and len(self.payload) >= 2:
            addr = int.from_bytes(self.payload[0:2], "big")
            return self.msg_type, f"addr=0x{addr:04x}"
        if self.msg_type == 0x45 and len(self.payload) >= 4:
            tag = self.payload[1:4].hex()
            return self.msg_type, f"tag=0x{tag}"
        return self.msg_type, "unknown"


def _parse_int(value: str) -> int:
    value = value.strip().lower()
    return int(value, 16) if value.startswith("0x") else int(value, 16 if any(c in value for c in "abcdef") else 10)


def _load_pushes(path: Path, *, msg_types: set[int], boiler_addr: int) -> list[PushFrame]:
    out: list[PushFrame] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"{path}:{line_no}: invalid JSON: {exc}", file=sys.stderr)
            continue
        if row.get("event") != "rf_frame":
            continue
        if row.get("control") != "0x05":
            continue
        if row.get("ack"):
            continue
        if _parse_int(str(row.get("from_addr", "0"))) != boiler_addr:
            continue
        msg_type = _parse_int(str(row.get("msg_type", "0")))
        if msg_types and msg_type not in msg_types:
            continue
        payload = bytes.fromhex(str(row.get("payload_hex", "")))
        req = _parse_int(str(row.get("request_id", "0")))
        rssi_raw = row.get("rssi")
        rssi = int(rssi_raw) if rssi_raw is not None else None
        out.append(
            PushFrame(
                source=f"{path.name}:{line_no}",
                ts=str(row.get("ts", "")),
                msg_type=msg_type,
                request_id=req,
                payload=payload,
                rssi=rssi,
            )
        )
    return out


def _stamp_guess(data: bytes) -> int | None:
    if len(data) < 4:
        return None
    return (data[2] << 24) + (data[3] << 16) + (data[0] << 8) + data[1]


def _print_matrix(frames: list[PushFrame], *, stamp_delta: bool) -> None:
    if not frames:
        return
    width = max(len(f.payload) for f in frames)
    varying = {
        idx
        for idx in range(width)
        if len({f.payload[idx] if idx < len(f.payload) else None for f in frames}) > 1
    }

    print(f"  frames: {len(frames)}  payload_len: {width}")
    header = "     " + "".join(f"{idx:02x} " for idx in range(width))
    print(header.rstrip())
    for frame in frames:
        cells: list[str] = []
        for idx in range(width):
            if idx >= len(frame.payload):
                cells.append("..")
                continue
            val = frame.payload[idx]
            mark = "*" if idx in varying else " "
            cells.append(f"{mark}{val:02x}")
        ts_short = frame.ts.replace("T", " ").replace("+00:00", "Z")[:19]
        rssi = "" if frame.rssi is None else f" rssi={frame.rssi}"
        print(f"  {ts_short} req=0x{frame.request_id:02x}{rssi}  {' '.join(cells)}")

    if stamp_delta and frames[0].msg_type == 0x45 and width >= 8:
        print("  stamp field (bytes 4-7, holiday byte order 2,3,0,1):")
        prev: tuple[str, int | None] | None = None
        for frame in frames:
            stamp = _stamp_guess(frame.payload[4:8])
            label = frame.ts
            if prev is not None and stamp is not None and prev[1] is not None:
                delta = stamp - prev[1]
                print(f"    {label}  raw={frame.payload[4:8].hex()}  stamp={stamp}  delta={delta}")
            else:
                print(f"    {label}  raw={frame.payload[4:8].hex()}  stamp={stamp}")
            prev = (label, stamp)


def _summarize(path: Path, frames: list[PushFrame]) -> None:
    if not frames:
        return
    ts_values = [f.ts for f in frames if f.ts]
    if len(ts_values) >= 2:
        t0 = datetime.fromisoformat(ts_values[0].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(ts_values[-1].replace("Z", "+00:00"))
        span = t1 - t0
        print(f"{path.name}: {len(frames)} push(es) over {span}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path, help="One or more .raw.jsonl capture files")
    parser.add_argument(
        "--msg-type",
        action="append",
        default=[],
        help="Filter message type (hex, e.g. 10 or 0x45). Default: 0x10 and 0x45",
    )
    parser.add_argument("--boiler-addr", default="0x80", help="Boiler RF address (default 0x80)")
    parser.add_argument(
        "--stamp-delta",
        action="store_true",
        help="For 0x45, print guessed stamp field and deltas between consecutive frames",
    )
    args = parser.parse_args(argv)

    msg_types = {_parse_int(value) for value in args.msg_type} if args.msg_type else {0x10, 0x45}

    boiler_addr = _parse_int(args.boiler_addr)
    all_frames: list[PushFrame] = []
    for path in args.logs:
        if not path.exists():
            print(f"missing file: {path}", file=sys.stderr)
            return 1
        frames = _load_pushes(path, msg_types=msg_types, boiler_addr=boiler_addr)
        _summarize(path, frames)
        all_frames.extend(frames)

    if not all_frames:
        print("No matching boiler push frames found.")
        return 0

    grouped: dict[tuple[int, str], list[PushFrame]] = defaultdict(list)
    for frame in all_frames:
        grouped[frame.group_key].append(frame)

    for (msg_type, label), frames in sorted(grouped.items()):
        print(f"\n=== msg_type=0x{msg_type:02x} {label} ===")
        _print_matrix(frames, stamp_delta=args.stamp_delta)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
