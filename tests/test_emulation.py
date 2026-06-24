"""Tests for the passive RF mirror."""

from __future__ import annotations

from frisquet_bridge.emulation import PassiveMirror
from frisquet_bridge.frame import MSG_READ, Frame
from frisquet_bridge.model import BoilerData, ZoneMode
from frisquet_bridge.transport.base import ReceivedFrame


def _received(frame: Frame, *, rssi: int = -43) -> ReceivedFrame:
    return ReceivedFrame(rssi=rssi, frame=frame, raw=frame.encode())


async def test_passive_mirror_learns_zone_config_from_satellite_broadcast() -> None:
    data = BoilerData()
    persisted: list[bool] = []
    mirror = PassiveMirror(data, boiler_addr=0x80, on_zone_config=lambda: persisted.append(True))

    # Satellite z1 reporting its own config to the boiler (0xa154 block),
    # exactly as captured from the real installation.
    frame = Frame(
        to_addr=0x80,
        from_addr=0x08,
        association_id=0xC0,
        request_id=0x70,
        control=0x7E,
        msg_type=0x17,
        payload=bytes.fromhex(
            "a1540015a154001830"
            "91821e05250000e0ffffffff00e0ffffff7f00e0ffffff7f"
            "00e0ffffff7f00e0ffffff7f00e0ffffff7f00e0ffffffff"
        ),
    )

    await mirror.handle(_received(frame))

    zone = data.zones[1]
    assert zone.mode == ZoneMode.AUTO
    assert zone.comfort_temperature is not None
    assert zone.reduced_temperature is not None
    assert zone.frost_temperature is not None
    assert zone.schedule is not None
    assert zone.schedule.days["sunday"].hex() == "00e0ffffffff"
    assert persisted == [True]


async def test_passive_mirror_learns_zone_config_from_relayed_write_ack() -> None:
    data = BoilerData()
    persisted: list[bool] = []
    mirror = PassiveMirror(data, boiler_addr=0x80, on_zone_config=lambda: persisted.append(True))

    # Boiler relaying the satellite's ACK of a Connect zone write back to
    # Connect (control=0x88); the body carries the applied schedule/setpoints.
    frame = Frame(
        to_addr=0x7E,
        from_addr=0x80,
        association_id=0xEA,
        request_id=0x54,
        control=0x88,
        msg_type=0x17,
        payload=bytes.fromhex(
            "3091821e05100000e0ffffffff00e0ffffff7f00e0ffffff7f"
            "00e0ffffff7f00e0ffffff7f00e0ffffff7f00e0ffffffff"
        ),
    )

    await mirror.handle(_received(frame, rssi=-82))

    zone = data.zones[1]
    assert zone.mode == ZoneMode.AUTO
    assert zone.comfort_temperature is not None
    assert zone.schedule is not None
    assert persisted == [True]


async def test_passive_mirror_ignores_satellite_info_as_zone_config() -> None:
    data = BoilerData()
    mirror = PassiveMirror(data, boiler_addr=0x80)

    # Satellite polling the boiler for consolidated state (0xa029) must not be
    # mistaken for a zone-config (0xa154) broadcast.
    frame = Frame(
        to_addr=0x80,
        from_addr=0x08,
        association_id=0xC0,
        request_id=0x6C,
        control=0x01,
        msg_type=0x17,
        payload=bytes.fromhex("a0290015a02f000408010d00c300250000"),
    )

    await mirror.handle(_received(frame))

    assert data.zones[1].schedule is None


async def test_passive_mirror_decodes_connect_read_response() -> None:
    data = BoilerData()
    mirror = PassiveMirror(data, boiler_addr=0x80)

    # A Connect box reads the sensor block from the boiler...
    request = Frame(
        to_addr=0x80,
        from_addr=0x7E,
        association_id=0x9C,
        request_id=0x1C,
        control=0x01,
        msg_type=MSG_READ,
        payload=bytes.fromhex("79e0001c"),
    )
    await mirror.handle(_received(request))

    # ...and the boiler answers (same association/request, ACK control).
    sensors = b"\x00" + (500).to_bytes(2, "big") + b"\x00" * 54
    response = Frame(
        to_addr=0x7E,
        from_addr=0x80,
        association_id=0x9C,
        request_id=0x1C,
        control=0x81,
        msg_type=MSG_READ,
        payload=sensors,
    )
    await mirror.handle(_received(response))

    assert data.boiler.dhw_temperature == 50.0


async def test_passive_mirror_learns_consigne_from_physical_satellite() -> None:
    data = BoilerData()
    mirror = PassiveMirror(data, boiler_addr=0x80)

    # Physical satellite z1 reporting ambient 20.0 / setpoint 19.0 in auto mode
    # to the boiler (read 0xa029, write 0xa02f).
    frame = Frame(
        to_addr=0x80,
        from_addr=0x08,
        association_id=0xC0,
        request_id=0x6C,
        control=0x01,
        msg_type=0x17,
        payload=bytes.fromhex("a0290015a02f00040800c800be00040000"),
    )

    await mirror.handle(_received(frame))

    zone = data.zones[1]
    assert zone.ambient_temperature == 20.0
    assert zone.setpoint_temperature == 19.0
    assert zone.mode == ZoneMode.AUTO
    assert zone.auto_comfort is False
