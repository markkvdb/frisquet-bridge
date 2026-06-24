"""Tests for Frisquet field encode/decode helpers."""

from __future__ import annotations

import pytest

from frisquet_bridge.connect.codec import (
    BoilerDate,
    decode_pressure16,
    decode_temp8,
    decode_temp16,
    encode_pressure16,
    encode_temp8,
    encode_temp16,
)


def test_temp16_round_trip() -> None:
    # Sonde test vector: 92 tenths -> 9.2 C
    assert decode_temp16(bytes.fromhex("005c")) == pytest.approx(9.2)
    assert encode_temp16(9.2) == bytes.fromhex("005c")


def test_temp8_round_trip() -> None:
    assert encode_temp8(18.0) == 130
    assert decode_temp8(130) == pytest.approx(18.0)


def test_temp8_clamps_out_of_range() -> None:
    assert encode_temp8(-10.0) == 0
    assert encode_temp8(100.0) == 255


def test_pressure16_round_trip() -> None:
    raw = encode_pressure16(1.5)
    assert decode_pressure16(raw) == pytest.approx(1.5, rel=1e-3)


def test_boiler_date_bcd_decode() -> None:
    # From frisquet-connect sonde reply test.
    d = BoilerDate.decode(bytes.fromhex("2304051131172803"))
    assert d.year == 2023
    assert d.month == 4
    assert d.day == 5
    assert d.hour == 11
    assert d.minute == 31
    assert d.second == 17
    assert d.weekday == 3
    assert d.isoformat() == "2023-04-05 11:31:17"
    assert d.slot == 11 * 2 + 31 // 30
