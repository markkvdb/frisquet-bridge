"""Pure encode/decode helpers for Frisquet field types (see Utils.h)."""

from __future__ import annotations

import struct
from dataclasses import dataclass


def decode_temp16(data: bytes) -> float:
    """Signed 16-bit big-endian, tenths of a degree."""
    return struct.unpack(">h", data[:2])[0] / 10.0


def encode_temp16(value: float) -> bytes:
    return struct.pack(">h", round(value * 10.0))


def decode_temp8(value: int) -> float:
    """One byte: 0 -> 5.0 C, 0.5 C steps."""
    return (value + 50) / 10.0


def encode_temp8(value: float) -> int:
    """Inverse of decode_temp8, clamped to the valid 5.0-30.5 C range."""
    raw = round(round(value * 2.0) / 2.0 * 10.0) - 50
    return max(0, min(255, raw))


def decode_pressure16(data: bytes) -> float:
    return struct.unpack(">h", data[:2])[0] / 5120.0


def encode_pressure16(value: float) -> bytes:
    return struct.pack(">h", round(value * 5120.0))


def _bcd(byte: int) -> int:
    return (byte >> 4) * 10 + (byte & 0x0F)


@dataclass(frozen=True, slots=True)
class BoilerDate:
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int
    weekday: int

    @classmethod
    def decode(cls, data: bytes) -> BoilerDate:
        # data = [year, month, day, hour, minute, second, _, weekday] (BCD)
        return cls(
            year=2000 + _bcd(data[0]),
            month=_bcd(data[1]),
            day=_bcd(data[2]),
            hour=_bcd(data[3]),
            minute=_bcd(data[4]),
            second=_bcd(data[5]),
            weekday=data[7] if len(data) > 7 else 0,
        )

    def isoformat(self) -> str:
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d} {self.hour:02d}:{self.minute:02d}:{self.second:02d}"

    @property
    def slot(self) -> int:
        """Half-hour slot index (0-47) used by the weekly schedule."""
        return self.hour * 2 + self.minute // 30
