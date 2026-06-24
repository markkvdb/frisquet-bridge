"""Tests for high-level boiler write operations."""

from __future__ import annotations

import asyncio

import pytest

from frisquet_bridge.climate import HVAC_HEAT, PRESET_BOOST
from frisquet_bridge.connect import ops as ops_module
from frisquet_bridge.connect.client import FrisquetClient
from frisquet_bridge.connect.ops import BoilerOps
from frisquet_bridge.connect.state import ProtocolState
from frisquet_bridge.frame import ADDR_SATELLITE_Z1, MSG_INIT, Frame
from frisquet_bridge.model import BoilerData, DhwMode, ZoneMode
from tests.helpers import FakeTransport, seed_zone_metadata


@pytest.fixture(autouse=True)
def _fast_zone_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Zone writes are fire-and-forget bursts; drop the inter-send delay so the
    # unit tests don't sleep in real time.
    monkeypatch.setattr(ops_module, "ZONE_WRITE_INTERVAL", 0.0)

_SATELLITE_INFO = bytes.fromhex(
    "2a050a000026062216274421010111005000100000000004f6000000000000000004f60000000000000000"
)


def _ops(fake_transport: FakeTransport, *, self_addr: int | None = None) -> BoilerOps:
    state = ProtocolState(
        network_id=bytes.fromhex("05d97f78"),
        association_id=0xEA,
        request_id=0x28,
    )
    kwargs = {} if self_addr is None else {"self_addr": self_addr}
    client = FrisquetClient(fake_transport, state, **kwargs)
    return BoilerOps(client, boiler_addr=0x80)


async def _ack_first_init(fake_transport: FakeTransport) -> None:
    while not fake_transport.sent:
        await asyncio.sleep(0)
    request = fake_transport.sent[0]
    fake_transport.push_frame(
        Frame(
            to_addr=request.from_addr,
            from_addr=request.to_addr,
            association_id=request.association_id,
            request_id=request.request_id,
            control=request.control | 0x80,
            msg_type=request.msg_type,
            payload=b"",
        )
    )


async def test_write_dhw_mode_sends_init_and_updates_model(fake_transport: FakeTransport) -> None:
    ops = _ops(fake_transport)
    data = BoilerData()
    data.boiler.dhw_frame_bits = 0x80

    task = asyncio.create_task(_ack_first_init(fake_transport))
    try:
        await ops.write_dhw_mode(data, DhwMode.ECO)
    finally:
        await task

    sent = fake_transport.sent[0]
    assert sent.msg_type == MSG_INIT
    assert sent.payload == bytes.fromhex("a0fc0001a0fc0001020088")
    assert data.boiler.dhw_mode == DhwMode.ECO


async def test_write_outside_temperature_keeps_boiler_observed_temperature_separate(fake_transport: FakeTransport) -> None:
    ops = _ops(fake_transport)
    data = BoilerData()
    data.boiler.outside_temperature = 8.0

    task = asyncio.create_task(_ack_first_init(fake_transport))
    try:
        await ops.write_outside_temperature(data, 12.34)
    finally:
        await task

    sent = fake_transport.sent[0]
    assert sent.msg_type == MSG_INIT
    assert sent.payload == bytes.fromhex("9c540004a029000102007b")
    assert data.sonde.outside_temperature == pytest.approx(12.3)
    assert data.boiler.outside_temperature == pytest.approx(8.0)


async def test_write_zone_mode_sends_short_area_init(fake_transport: FakeTransport) -> None:
    ops = _ops(fake_transport)
    data = BoilerData()
    seed_zone_metadata(data, 1)

    task = asyncio.create_task(_ack_first_init(fake_transport))
    try:
        await ops.write_zone_mode(1, data, ZoneMode.COMFORT)
    finally:
        await task

    sent = fake_transport.sent[0]
    assert sent.control == 0x08
    assert sent.msg_type == MSG_INIT
    assert sent.payload == bytes.fromhex(
        "a1540018a154001830967814062500"
        "00e0ffffffff"
        "00e0ffffff7f"
        "00e0ffffff7f"
        "00e0ffffff7f"
        "00e0ffffff7f"
        "00e0ffffff7f"
        "00e0ffffffff"
    )
    assert data.zones[1].mode == ZoneMode.COMFORT
    assert data.zones[1].boost is False


async def test_write_zone_auto_uses_schedule_options_not_learned_bits(fake_transport: FakeTransport) -> None:
    ops = _ops(fake_transport)
    data = BoilerData()
    seed_zone_metadata(data, 1)
    data.zones[1].mode_options = 0x20  # previously-learned frost options

    task = asyncio.create_task(_ack_first_init(fake_transport))
    try:
        await ops.write_zone_mode(1, data, ZoneMode.AUTO)
    finally:
        await task

    sent = fake_transport.sent[0]
    # body = comfort, reduced, frost, mode(0x05 auto), opts, 0x00. The options
    # byte must be the schedule form 0x10, not the stale frost form 0x20.
    assert sent.payload[9:15].hex() == "967814051000"


async def test_write_zone_short_omits_schedule_when_unknown(fake_transport: FakeTransport) -> None:
    ops = _ops(fake_transport)
    data = BoilerData()
    zs = data.zones[1]
    zs.comfort_temperature = 20.0
    zs.reduced_temperature = 17.0
    zs.frost_temperature = 7.0
    zs.mode_options = 0x24  # no schedule learned yet

    task = asyncio.create_task(_ack_first_init(fake_transport))
    try:
        await ops.write_zone_mode(1, data, ZoneMode.COMFORT)
    finally:
        await task

    sent = fake_transport.sent[0]
    assert sent.control == 0x08
    # OpenFrisquetVisio-style short write: write size 0x0003, 6-byte body, no schedule.
    assert sent.payload == bytes.fromhex("a1540018a154000306967814062500")
    assert data.zones[1].mode == ZoneMode.COMFORT


async def test_send_zone_consigne_emits_satellite_write(fake_transport: FakeTransport) -> None:
    fake_transport.init_responses = {0x01: _SATELLITE_INFO}
    ops = _ops(fake_transport, self_addr=ADDR_SATELLITE_Z1)
    data = BoilerData()
    zs = data.zones[1]
    zs.mode = ZoneMode.AUTO
    zs.auto_comfort = False

    await ops.send_zone_consigne(1, data, ambient=20.0, setpoint=19.0)

    sent = fake_transport.sent[0]
    assert sent.from_addr == ADDR_SATELLITE_Z1
    assert sent.msg_type == MSG_INIT
    # read 0xa029/0x15, write 0xa02f/0x04, data len 8: ambient, setpoint, 00, mode=0x04, options 0x0000
    assert sent.payload == bytes.fromhex("a0290015a02f00040800c800be00040000")
    # the boiler-state block in the response is decoded back into the model
    assert data.zones[1].ambient_temperature == pytest.approx(27.3)


async def test_write_zone_boost_requires_comfort_mode(fake_transport: FakeTransport) -> None:
    ops = _ops(fake_transport)
    data = BoilerData()
    seed_zone_metadata(data, 1)
    data.zones[1].mode = ZoneMode.AUTO

    with pytest.raises(ValueError, match="Comfort"):
        await ops.write_zone_boost(1, data, True)


async def test_write_zone_override_forces_auto_derogation(fake_transport: FakeTransport) -> None:
    ops = _ops(fake_transport)
    data = BoilerData()
    seed_zone_metadata(data, 1)

    task = asyncio.create_task(_ack_first_init(fake_transport))
    try:
        await ops.write_zone_override(1, data, "reduced")
    finally:
        await task

    sent = fake_transport.sent[0]
    # AUTO + eco derogation: mode byte stays 0x05 (auto), options switch to the
    # fixed-level form 0x24 (eco) instead of the schedule form 0x10.
    assert sent.payload == bytes.fromhex(
        "a1540018a154001830967814052400"
        "00e0ffffffff"
        "00e0ffffff7f"
        "00e0ffffff7f"
        "00e0ffffff7f"
        "00e0ffffff7f"
        "00e0ffffff7f"
        "00e0ffffffff"
    )
    assert data.zones[1].mode == ZoneMode.AUTO
    assert data.zones[1].override is True
    assert data.zones[1].auto_comfort is False


async def test_read_satellite_info_populates_zone_mode() -> None:
    payload = bytes.fromhex("2a050a000026062216274421010111005000100000000004f6000000000000000004f60000000000000000")
    transport = FakeTransport(init_responses={0x01: payload})
    ops = _ops(transport)
    data = BoilerData()

    await ops.read_satellite_info(data)

    assert data.zones[1].mode == ZoneMode.FROST
    assert data.zones[1].ambient_temperature == pytest.approx(27.3)


async def test_write_zone_raises_when_metadata_not_yet_learned() -> None:
    transport = FakeTransport()
    ops = _ops(transport)
    data = BoilerData()

    with pytest.raises(ValueError, match="not yet learned"):
        await ops.write_zone_mode(1, data, ZoneMode.COMFORT)

    assert transport.sent == []


async def test_apply_zone_climate_boost(fake_transport: FakeTransport) -> None:
    ops = _ops(fake_transport)
    data = BoilerData()
    seed_zone_metadata(data, 1)
    data.zones[1].mode = ZoneMode.COMFORT

    task = asyncio.create_task(_ack_first_init(fake_transport))
    try:
        await ops.apply_zone_climate(1, data, hvac_mode=HVAC_HEAT, preset=PRESET_BOOST)
    finally:
        await task

    assert data.zones[1].mode == ZoneMode.COMFORT
    assert data.zones[1].boost is True


async def test_apply_zone_climate_target_temperature(fake_transport: FakeTransport) -> None:
    ops = _ops(fake_transport)
    data = BoilerData()
    seed_zone_metadata(data, 1)
    data.zones[1].mode = ZoneMode.COMFORT

    task = asyncio.create_task(_ack_first_init(fake_transport))
    try:
        await ops.apply_zone_climate(1, data, target_temperature=21.0)
    finally:
        await task

    assert data.zones[1].comfort_temperature == pytest.approx(21.0)
