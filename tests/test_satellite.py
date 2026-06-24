"""Tests for the virtual satellite runner."""

from __future__ import annotations

from frisquet_bridge.connect.client import FrisquetClient
from frisquet_bridge.connect.ops import BoilerOps
from frisquet_bridge.connect.state import ProtocolState
from frisquet_bridge.frame import ADDR_SATELLITE_Z1
from frisquet_bridge.model import BoilerData, ZoneMode
from frisquet_bridge.satellite import VirtualSatellite
from tests.helpers import FakeTransport

_SATELLITE_INFO = bytes.fromhex(
    "2a050a000026062216274421010111005000100000000004f6000000000000000004f60000000000000000"
)


def _satellite(data: BoilerData, transport: FakeTransport) -> VirtualSatellite:
    state = ProtocolState(network_id=bytes.fromhex("05d97f78"), association_id=0xC0, request_id=0x6C)
    client = FrisquetClient(transport, state, self_addr=ADDR_SATELLITE_Z1)
    ops = BoilerOps(client, boiler_addr=0x80)
    return VirtualSatellite(1, ops, data)


async def test_send_now_skips_when_ambient_unknown() -> None:
    transport = FakeTransport(init_responses={0x01: _SATELLITE_INFO})
    data = BoilerData()
    data.zones[1].mode = ZoneMode.AUTO  # ambient still unknown
    satellite = _satellite(data, transport)

    assert await satellite.send_now() is False
    assert transport.sent == []


async def test_send_now_emits_consigne_when_ready() -> None:
    transport = FakeTransport(init_responses={0x01: _SATELLITE_INFO})
    data = BoilerData()
    zs = data.zones[1]
    zs.mode = ZoneMode.AUTO
    zs.auto_comfort = False
    zs.reduced_temperature = 19.0
    zs.reported_ambient = 20.0
    satellite = _satellite(data, transport)

    assert await satellite.send_now() is True
    sent = transport.sent[0]
    assert sent.from_addr == ADDR_SATELLITE_Z1
    # read 0xa029/0x15, write 0xa02f/0x04, data len 8.
    assert sent.payload[:9].hex() == "a0290015a02f000408"
    # ambient 20.0 and active setpoint 19.0 (reduced) encoded as temperature16.
    assert sent.payload[9:13].hex() == "00c800be"
