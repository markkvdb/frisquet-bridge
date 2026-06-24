"""Tests for Frisquet RF frame encode/decode."""

from __future__ import annotations

import pytest

from frisquet_bridge.frame import (
    MSG_ASSOCIATION,
    Frame,
    FrameError,
    device_name,
)

# Association broadcast from frisquet-connect pair.rs test.
ASSOCIATION_HEX = "0b008012d402410412345678"


def test_decode_association_frame_from_rust_vector() -> None:
    frame = Frame.decode(bytes.fromhex(ASSOCIATION_HEX))
    assert frame.to_addr == 0x00
    assert frame.from_addr == 0x80
    assert frame.association_id == 0x12
    assert frame.request_id == 0xD4
    assert not frame.is_ack
    assert frame.control == 0x02
    assert frame.msg_type == MSG_ASSOCIATION
    assert frame.payload == bytes.fromhex("0412345678")


def test_encode_sensors_read_request() -> None:
    frame = Frame(
        to_addr=0x80,
        from_addr=0x7E,
        association_id=0x9C,
        request_id=0x18,
        control=0x01,
        msg_type=0x03,
        payload=bytes.fromhex("79e0001c"),
    )
    assert frame.encode().hex() == "0a807e9c18010379e0001c"


def test_round_trip_encode_decode() -> None:
    original = Frame.decode(bytes.fromhex(ASSOCIATION_HEX))
    assert Frame.decode(original.encode()) == original


def test_decode_too_short_raises() -> None:
    with pytest.raises(FrameError, match="too short"):
        Frame.decode(b"\x01\x02")


def test_decode_truncated_raises() -> None:
    # length=0x0b implies 12 bytes; provide only the 7-byte header.
    with pytest.raises(FrameError, match="truncated"):
        Frame.decode(bytes.fromhex("0b008012d40241"))


def test_encode_payload_too_long_raises() -> None:
    frame = Frame(
        to_addr=0x80,
        from_addr=0x7E,
        association_id=0x01,
        request_id=0x02,
        control=0x01,
        msg_type=0x03,
        payload=b"\x00" * 250,
    )
    with pytest.raises(FrameError, match="too long"):
        frame.encode()


def test_matches_response_fields() -> None:
    req = Frame(
        to_addr=0x80,
        from_addr=0x7E,
        association_id=0x9C,
        request_id=0x18,
        control=0x01,
        msg_type=0x03,
        payload=b"",
    )
    resp = Frame(
        to_addr=0x7E,
        from_addr=0x80,
        association_id=0x9C,
        request_id=0x18,
        control=0x81,
        msg_type=0x03,
        payload=b"\xaa",
    )
    assert resp.matches(
        from_addr=0x80,
        to_addr=0x7E,
        association_id=0x9C,
        request_id=0x18,
    )
    assert not req.matches(
        from_addr=0x80,
        to_addr=0x7E,
        association_id=0x9C,
        request_id=0x18,
    )


def test_ack_flag_comes_from_control_byte() -> None:
    frame = Frame(
        to_addr=0x7E,
        from_addr=0x80,
        association_id=0x9C,
        request_id=0xF4,
        control=0x81,
        msg_type=0x03,
        payload=b"",
    )
    assert frame.is_ack


def test_device_name_known_and_unknown() -> None:
    assert "boiler" in device_name(0x80)
    assert device_name(0xAB) == "0xab"
