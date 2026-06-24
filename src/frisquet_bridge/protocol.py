"""Host <-> modem serial line protocol helpers (see docs/PROTOCOL.md).

This module only deals with the ASCII line framing exchanged over USB serial
with the Feather modem. The RF frame model lives in `frame.py`.
"""

from __future__ import annotations

import glob
import re
from dataclasses import dataclass

# Modem -> host line matchers.
RX_RE = re.compile(r"^RX (-?\d+) ([0-9a-fA-F]+) ([0-9a-fA-F]{2})$")
OK_RE = re.compile(r"^OK (\d+)$")
ERR_RE = re.compile(r"^ERR (\d+) (\S+)$")
PONG_RE = re.compile(r"^PONG (\d+)$")
INFO_RE = re.compile(r"^INFO (\S+) (.*)$")
READY_RE = re.compile(r"^READY (\S+) (\S+)$")


def crc8(data: bytes) -> int:
    """XOR-based CRC8 over the ASCII bytes, matching the firmware."""
    c = 0
    for b in data:
        c ^= b
    return c


def format_cmd(line: str) -> str:
    """Append the CRC and newline to a host command line."""
    payload = line.encode("ascii")
    return f"{line} {crc8(payload):02x}\n"


def verify_crc(line: str) -> bool:
    """Check the trailing ` <crc>` token of a received line."""
    head, _, crc = line.rpartition(" ")
    if not head or len(crc) != 2:
        return False
    try:
        expected = int(crc, 16)
    except ValueError:
        return False
    return crc8(head.encode("ascii")) == expected


@dataclass(frozen=True, slots=True)
class RxLine:
    rssi: int
    frame_hex: str


def parse_rx(line: str) -> RxLine | None:
    """Parse an `RX <rssi> <hex> <crc>` line (CRC already validated upstream)."""
    m = RX_RE.match(line)
    if not m:
        return None
    return RxLine(rssi=int(m.group(1)), frame_hex=m.group(2))


def default_serial_port() -> str:
    for pattern in (
        "/dev/serial/by-id/usb-Adafruit*Feather_M0*",
        "/dev/serial/by-id/usb-Adafruit*",
    ):
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return "/dev/ttyACM0"
