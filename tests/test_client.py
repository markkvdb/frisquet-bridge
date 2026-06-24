"""Tests for FrisquetClient request/response and pairing."""

from __future__ import annotations

import asyncio

import pytest

from frisquet_bridge.connect.client import FrisquetClient
from frisquet_bridge.connect.state import ProtocolState
from frisquet_bridge.frame import (
    ADDR_BOILER,
    ADDR_BROADCAST,
    ADDR_CONNECT,
    MSG_ASSOCIATION,
    MSG_READ,
    Frame,
)
from frisquet_bridge.transport.base import TransportError
from tests.helpers import FakeTransport


@pytest.fixture
def protocol_state() -> ProtocolState:
    return ProtocolState(
        network_id=bytes.fromhex("05d97f78"),
        association_id=0x9C,
        request_id=0x18,
    )


@pytest.fixture
def client(fake_transport: FakeTransport, protocol_state: ProtocolState) -> FrisquetClient:
    return FrisquetClient(fake_transport, protocol_state)


async def test_read_memory_happy_path(
    client: FrisquetClient,
    fake_transport: FakeTransport,
    protocol_state: ProtocolState,
) -> None:
    payload = await client.read_memory(0x79E0, 0x1C)
    assert payload == bytes.fromhex("38024b0269")
    assert fake_transport.sent[0].msg_type == MSG_READ
    assert fake_transport.sent[0].payload == bytes.fromhex("79e0001c")
    assert fake_transport.sent[0].request_id == 0x1C
    assert protocol_state.request_id == 0x1C
    assert fake_transport.network_ids[-1] == protocol_state.network_id


async def test_request_retries_then_raises(
    fake_transport: FakeTransport,
    protocol_state: ProtocolState,
) -> None:
    fake_transport.auto_reply = False
    client = FrisquetClient(fake_transport, protocol_state)

    with pytest.raises(TransportError, match="failed after"):
        await client.request(
            control=0x01,
            msg_type=MSG_READ,
            payload=bytes.fromhex("79e0001c"),
            timeout=0.01,
            retries=2,
        )

    assert len(fake_transport.sent) == 2


async def test_request_ignores_non_ack_frame_with_same_ids(
    fake_transport: FakeTransport,
    protocol_state: ProtocolState,
) -> None:
    fake_transport.auto_reply = False
    client = FrisquetClient(fake_transport, protocol_state)

    async def push_noise_then_response() -> None:
        while not fake_transport.sent:
            await asyncio.sleep(0)
        request = fake_transport.sent[0]
        fake_transport.push_frame(
            Frame(
                to_addr=request.from_addr,
                from_addr=request.to_addr,
                association_id=request.association_id,
                request_id=request.request_id,
                control=0x01,
                msg_type=request.msg_type,
                payload=bytes.fromhex("ffffffff"),
            )
        )
        fake_transport.push_frame(
            Frame(
                to_addr=request.from_addr,
                from_addr=request.to_addr,
                association_id=request.association_id,
                request_id=request.request_id,
                control=0x81,
                msg_type=request.msg_type,
                payload=bytes.fromhex("38024b0269"),
            )
        )

    task = asyncio.create_task(push_noise_then_response())
    try:
        payload = await client.read_memory(0x79E0, 0x1C)
    finally:
        await task

    assert payload == bytes.fromhex("38024b0269")


async def test_request_send_failure_retries(
    fake_transport: FakeTransport,
    protocol_state: ProtocolState,
) -> None:
    fake_transport.send_raises = TransportError("modem offline")
    client = FrisquetClient(fake_transport, protocol_state)

    with pytest.raises(TransportError, match="failed after"):
        await client.request(
            control=0x01,
            msg_type=MSG_READ,
            payload=bytes.fromhex("79e0001c"),
            retries=2,
        )

    assert fake_transport.sent == []


async def test_pair_success(
    fake_transport: FakeTransport,
    protocol_state: ProtocolState,
) -> None:
    fake_transport.auto_reply = False
    client = FrisquetClient(fake_transport, protocol_state)

    async def push_then_quiet() -> None:
        await asyncio.sleep(0.05)
        broadcast = Frame(
            to_addr=ADDR_BROADCAST,
            from_addr=ADDR_BOILER,
            association_id=0x12,
            request_id=0xD4,
            control=0x02,
            msg_type=MSG_ASSOCIATION,
            payload=bytes.fromhex("0412345678"),
        )
        fake_transport.push_frame(broadcast)

    task = asyncio.create_task(push_then_quiet())
    try:
        assoc = await client.pair(ADDR_CONNECT, listen_window=0.2)
    finally:
        await task

    assert assoc.network_id == bytes.fromhex("12345678")
    assert assoc.association_id == 0x12
    assert assoc.request_id == 0xD4
    assert fake_transport.network_ids[0] == b"\xff\xff\xff\xff"
    reply = fake_transport.sent[0]
    assert reply.to_addr == ADDR_BOILER
    assert reply.from_addr == ADDR_CONNECT
    assert reply.control == 0x82
    assert reply.msg_type == MSG_ASSOCIATION


async def test_pair_timeout(
    fake_transport: FakeTransport,
    protocol_state: ProtocolState,
) -> None:
    fake_transport.auto_reply = False
    client = FrisquetClient(fake_transport, protocol_state)

    with pytest.raises(TransportError, match="pairing timed out"):
        await client.pair(ADDR_CONNECT, listen_window=0.05)


async def test_recover_network_id_does_not_reply(
    fake_transport: FakeTransport,
    protocol_state: ProtocolState,
) -> None:
    fake_transport.auto_reply = False
    client = FrisquetClient(fake_transport, protocol_state)

    async def push_broadcast() -> None:
        await asyncio.sleep(0.02)
        fake_transport.push_frame(
            Frame(
                to_addr=ADDR_BROADCAST,
                from_addr=ADDR_BOILER,
                association_id=0x12,
                request_id=0xD4,
                control=0x02,
                msg_type=MSG_ASSOCIATION,
                payload=bytes.fromhex("0412345678"),
            )
        )

    task = asyncio.create_task(push_broadcast())
    try:
        network_id = await client.recover_network_id(timeout=0.3)
    finally:
        await task

    assert network_id == bytes.fromhex("12345678")
    assert fake_transport.sent == []  # recovery never transmits


async def test_recover_network_id_timeout(
    fake_transport: FakeTransport,
    protocol_state: ProtocolState,
) -> None:
    fake_transport.auto_reply = False
    client = FrisquetClient(fake_transport, protocol_state)

    with pytest.raises(TransportError, match="network id recovery timed out"):
        await client.recover_network_id(timeout=0.05)


async def test_fake_transport_rejects_bad_network_id(
    fake_transport: FakeTransport,
) -> None:
    with pytest.raises(TransportError, match="4 bytes"):
        await fake_transport.set_network_id(b"\x01")
