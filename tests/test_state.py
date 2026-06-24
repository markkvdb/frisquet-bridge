"""Tests for protocol state (request id management)."""

from __future__ import annotations

from frisquet_bridge.connect.state import ProtocolState


def test_next_request_id_increments_by_four() -> None:
    state = ProtocolState(
        network_id=bytes.fromhex("05d97f78"),
        association_id=0x9C,
        request_id=0x18,
    )
    assert state.next_request_id() == 0x1C
    assert state.request_id == 0x1C


def test_next_request_id_wraps_at_256() -> None:
    state = ProtocolState(
        network_id=bytes.fromhex("05d97f78"),
        association_id=0x9C,
        request_id=0xFE,
    )
    assert state.next_request_id() == 0x02


def test_on_change_called() -> None:
    seen: list[int] = []

    def hook(st: ProtocolState) -> None:
        seen.append(st.request_id)

    state = ProtocolState(
        network_id=bytes.fromhex("05d97f78"),
        association_id=0x9C,
        request_id=0x10,
        on_change=hook,
    )
    state.next_request_id()
    assert seen == [0x14]
