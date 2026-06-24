"""Tests for host ↔ modem serial line protocol helpers."""

from __future__ import annotations

from frisquet_bridge.protocol import (
    crc8,
    format_cmd,
    parse_rx,
    verify_crc,
)


def test_crc8_xor_accumulator() -> None:
    assert crc8(b"") == 0
    assert crc8(b"abc") == ord("a") ^ ord("b") ^ ord("c")


def test_format_cmd_appends_crc_and_newline() -> None:
    line = format_cmd("PING 1")
    assert line.endswith("\n")
    assert verify_crc(line.strip())


def test_verify_crc_rejects_bad_crc() -> None:
    assert verify_crc("PING 1 ff") is False


def test_verify_crc_rejects_missing_token() -> None:
    assert verify_crc("PING") is False
    assert verify_crc("") is False


def test_verify_crc_rejects_non_hex() -> None:
    assert verify_crc("PING 1 zz") is False


def test_parse_rx_valid_line() -> None:
    rx = parse_rx("RX -78 deadbeef 00")
    assert rx is not None
    assert rx.rssi == -78
    assert rx.frame_hex == "deadbeef"


def test_parse_rx_invalid_line() -> None:
    assert parse_rx("OK 1") is None
    assert parse_rx("HB") is None
