"""Tests for sensor/consumption decode."""

from __future__ import annotations

import pytest

from frisquet_bridge.connect.decode import (
    BOILER_EVENT_KIND_CLEAR,
    BOILER_EVENT_KIND_SET,
    decode_clock,
    decode_consumption,
    decode_daily_consumption,
    decode_dhw_mode,
    decode_holiday_push,
    decode_memory_push,
    decode_satellite_info,
    decode_sensors,
    decode_zone_consigne,
    decode_zone_init,
    describe_boiler_event,
    describe_memory_push,
    encode_satellite_mode,
    parse_boiler_event_push,
    parse_memory_push,
)
from frisquet_bridge.connect.passive import PassiveReadTracker
from frisquet_bridge.frame import MSG_BOILER_EVENT, MSG_MEMORY_PUSH, Frame
from frisquet_bridge.model import BoilerData, DhwMode, ZoneMode, ZoneState


def _sensors_payload() -> bytes:
    # Minimal synthetic block: len=0x38 + zeros, DHW=45.0°C (450), ext=129 at end
    payload = bytearray(57)
    payload[0] = 0x38
    payload[1:3] = (450).to_bytes(2, "big", signed=True)
    payload[55:57] = (1290).to_bytes(2, "big", signed=True)  # 129.0°C sentinel
    return bytes(payload)


def test_decode_sensors_dhw_and_outside_sentinel() -> None:
    data = BoilerData()
    decode_sensors(_sensors_payload(), data)
    assert data.boiler.dhw_temperature == pytest.approx(45.0)
    assert data.boiler.outside_temperature is None


def test_decode_sensors_zone_absent_temperature_sentinel() -> None:
    payload = bytearray(_sensors_payload())
    payload[7:9] = (1270).to_bytes(2, "big", signed=True)
    payload[39:41] = (1270).to_bytes(2, "big", signed=True)
    data = BoilerData()

    decode_sensors(bytes(payload), data)

    assert data.zones[2].flow_temperature is None
    assert data.zones[2].ambient_temperature is None


def test_decode_sensors_rich_connect_block() -> None:
    payload = bytes.fromhex(
        "38028802b1012004f604f6020500000000000000001a0000050159"
        "00000000000000000000011104f604f600c600c600c6005000000000050a"
    )
    data = BoilerData()

    decode_sensors(payload, data)

    assert data.boiler.dhw_temperature == pytest.approx(64.8)
    assert data.boiler.cdc_temperature == pytest.approx(68.9)
    assert data.boiler.cdc_safety_temperature == pytest.approx(51.7)
    assert data.boiler.flue_temperature == pytest.approx(0.0)
    assert data.boiler.dhw_power == pytest.approx(0.0)
    assert data.boiler.heating_power == pytest.approx(0.0)
    assert data.boiler.dhw_instant_temperature == pytest.approx(34.5)
    assert data.boiler.pressure == pytest.approx(1.3, rel=1e-3)
    assert data.boiler.outside_temperature is None
    assert data.zones[1].ambient_temperature == pytest.approx(27.3)
    assert data.zones[1].flow_setpoint_temperature == pytest.approx(19.8)
    assert data.zones[1].setpoint_temperature == pytest.approx(8.0)
    assert data.zones[2].ambient_temperature is None


def test_decode_sensors_does_not_overwrite_virtual_sonde_target() -> None:
    data = BoilerData()
    data.sonde.outside_temperature = 18.5
    payload = bytearray(_sensors_payload())
    payload[55:57] = (172).to_bytes(2, "big", signed=True)

    decode_sensors(bytes(payload), data)

    assert data.boiler.outside_temperature == pytest.approx(17.2)
    assert data.sonde.outside_temperature == pytest.approx(18.5)


def test_decode_sensors_filters_implausible_values() -> None:
    data = BoilerData()
    payload = bytearray(57)
    payload[0] = 0x38
    payload[1:3] = (582).to_bytes(2, "big", signed=True)
    payload[3:5] = (1654).to_bytes(2, "big", signed=True)
    payload[37:39] = (544).to_bytes(2, "big", signed=True)
    payload[55:57] = (2581).to_bytes(2, "big", signed=True)

    decode_sensors(bytes(payload), data)

    assert data.boiler.dhw_temperature == pytest.approx(58.2)
    assert data.boiler.cdc_temperature is None
    assert data.zones[1].ambient_temperature is None
    assert data.boiler.outside_temperature is None


def test_decode_consumption() -> None:
    payload = bytearray(57)
    payload[0] = 0x38
    payload[1:3] = (100).to_bytes(2, "big", signed=True)
    payload[3:5] = (200).to_bytes(2, "big", signed=True)
    data = BoilerData()
    decode_consumption(bytes(payload), data)
    assert data.boiler.dhw_consumption == 100
    assert data.boiler.heating_consumption == 200


def test_decode_daily_consumption() -> None:
    payload = bytearray(57)
    payload[0] = 0x38
    payload[19:21] = (5).to_bytes(2, "big", signed=True)
    payload[21:23] = (0).to_bytes(2, "big", signed=True)
    data = BoilerData()
    decode_daily_consumption(bytes(payload), data)
    assert data.boiler.daily_dhw_consumption == 5
    assert data.boiler.daily_heating_consumption == 0
    assert data.boiler.dhw_consumption is None
    assert data.boiler.heating_consumption is None


def test_passive_read_tracker_tracks_consumption_response() -> None:
    tracker = PassiveReadTracker()
    request = Frame(
        to_addr=0x80,
        from_addr=0x7E,
        association_id=0x9C,
        request_id=0x34,
        control=0x01,
        msg_type=0x03,
        payload=bytes.fromhex("7a34001c"),
    )
    response_payload = bytearray(57)
    response_payload[0] = 0x38
    response_payload[1:3] = (123).to_bytes(2, "big", signed=True)
    response_payload[3:5] = (456).to_bytes(2, "big", signed=True)
    response = Frame(
        to_addr=0x7E,
        from_addr=0x80,
        association_id=0x9C,
        request_id=0x34,
        control=0x81,
        msg_type=0x03,
        payload=bytes(response_payload),
    )

    assert "consumption" in (tracker.describe(request) or "")
    assert tracker.describe(response) == "CONSUMPTION dhw=123 kWh heating=456 kWh"


def test_passive_read_tracker_tracks_daily_consumption_response() -> None:
    tracker = PassiveReadTracker()
    request = Frame(
        to_addr=0x80,
        from_addr=0x7E,
        association_id=0xEA,
        request_id=0x80,
        control=0x01,
        msg_type=0x03,
        payload=bytes.fromhex("7a18001c"),
    )
    response = Frame(
        to_addr=0x7E,
        from_addr=0x80,
        association_id=0xEA,
        request_id=0x80,
        control=0x81,
        msg_type=0x03,
        payload=bytes.fromhex(
            "3810014b19143c4b0f14504b0f145007020064000500000000000000"
            "0000000000803100000000000000000000000000000184138001101080"
        ),
    )

    assert tracker.describe(request) == "READ request addr=0x7a18 size=0x001c (daily_consumption)"
    assert tracker.describe(response) == "DAILY_CONSUMPTION dhw=5 kWh heating=0 kWh"


def test_passive_read_tracker_labels_non_consumption_read_response() -> None:
    tracker = PassiveReadTracker()
    request = Frame(
        to_addr=0x80,
        from_addr=0x7E,
        association_id=0x9C,
        request_id=0x38,
        control=0x01,
        msg_type=0x03,
        payload=bytes.fromhex("79e0001c"),
    )
    response = Frame(
        to_addr=0x7E,
        from_addr=0x80,
        association_id=0x9C,
        request_id=0x38,
        control=0x81,
        msg_type=0x03,
        payload=bytes(57),
    )

    assert "sensors" in (tracker.describe(request) or "")
    assert tracker.describe(response) == "SENSORS dhw=0.0°C cdc=0.0°C dhw_power=0.0kW heating_power=0.0kW pressure=0.0bar"


def test_decode_dhw_mode_preserves_frame_bits() -> None:
    data = BoilerData()
    decode_dhw_mode(bytes((0x01, 0x00, 0x88)), data)
    assert data.boiler.dhw_mode == DhwMode.ECO
    assert data.boiler.dhw_frame_bits == 0x80


def test_decode_clock() -> None:
    data = BoilerData()

    decode_clock(bytes.fromhex("082606221544492101"), data)

    assert data.last_seen_date == "2026-06-22 15:44:49"


def test_decode_satellite_info() -> None:
    data = BoilerData()
    payload = bytes.fromhex("2a050a000026062216274421010111005000100000000004f6000000000000000004f60000000000000000")

    decode_satellite_info(payload, data)

    assert data.last_seen_date == "2026-06-22 16:27:44"
    assert data.boiler.outside_temperature is None
    assert data.zones[1].ambient_temperature == pytest.approx(27.3)
    assert data.zones[1].setpoint_temperature == pytest.approx(8.0)
    assert data.zones[1].mode == ZoneMode.FROST
    assert data.zones[2].mode is None


def test_decode_satellite_info_does_not_overwrite_virtual_sonde_target() -> None:
    data = BoilerData()
    data.sonde.outside_temperature = 18.5
    payload = bytearray(
        bytes.fromhex("2a050a000026062216274421010111005000100000000004f6000000000000000004f60000000000000000")
    )
    payload[1:3] = (172).to_bytes(2, "big", signed=True)

    decode_satellite_info(bytes(payload), data)

    assert data.boiler.outside_temperature == pytest.approx(17.2)
    assert data.sonde.outside_temperature == pytest.approx(18.5)


def test_decode_zone_init_long_payload() -> None:
    data = BoilerData()
    payload = bytes.fromhex(
        "a1540015a154001830"
        "91821e08100000e0ffffffff00e0ffffff7f00e0ffffff7f"
        "00e0ffffff7f00e0ffffff7f00e0ffffff7f00e0ffffffff"
    )

    decode_zone_init(payload, 1, data)

    zone = data.zones[1]
    assert zone.comfort_temperature == pytest.approx(19.5)
    assert zone.reduced_temperature == pytest.approx(18.0)
    assert zone.frost_temperature == pytest.approx(8.0)
    assert zone.mode == ZoneMode.FROST
    assert zone.mode_options == 0x10
    assert zone.boost is False
    assert zone.override is False
    assert zone.schedule is not None
    assert zone.schedule.days["sunday"].hex() == "00e0ffffffff"


def test_passive_read_tracker_decodes_zone_init() -> None:
    tracker = PassiveReadTracker()
    frame = Frame(
        to_addr=0x80,
        from_addr=0x08,
        association_id=0xC0,
        request_id=0xF4,
        control=0x7E,
        msg_type=0x17,
        payload=bytes.fromhex(
            "a1540015a154001830"
            "91821e08100000e0ffffffff00e0ffffff7f00e0ffffff7f"
            "00e0ffffff7f00e0ffffff7f00e0ffffff7f00e0ffffffff"
        ),
    )

    description = tracker.describe(frame)

    assert description is not None
    assert description.startswith("ZONE1 mode=Frost protection boost=off comfort=19.5°C reduced=18.0°C frost=8.0°C")
    assert "schedule_raw=sun=00e0ffffffff" in description


def test_passive_read_tracker_decodes_satellite_info_without_zone_config_false_positive() -> None:
    tracker = PassiveReadTracker()
    request = Frame(
        to_addr=0x80,
        from_addr=0x08,
        association_id=0xC0,
        request_id=0x0C,
        control=0x01,
        msg_type=0x17,
        payload=bytes.fromhex("a0290015a02f0004080111005000100000"),
    )
    response = Frame(
        to_addr=0x08,
        from_addr=0x80,
        association_id=0xC0,
        request_id=0x0C,
        control=0x81,
        msg_type=0x17,
        payload=bytes.fromhex("2a050a000026062216274421010111005000100000000004f6000000000000000004f60000000000000000"),
    )

    assert tracker.describe(request) == "INIT request addr=0xa029 (satellite_info)"
    description = tracker.describe(response)

    assert description is not None
    assert description.startswith("SATELLITE_INFO clock=2026-06-22 16:27:44")
    assert "z1=mode:Frost protection amb:27.3°C setpoint:8.0°C" in description
    assert "ZONE1 mode=unknown" not in description


def test_passive_read_tracker_decodes_sensors_app_alias() -> None:
    tracker = PassiveReadTracker()
    request = Frame(
        to_addr=0x80,
        from_addr=0x7E,
        association_id=0xEA,
        request_id=0x74,
        control=0x01,
        msg_type=0x03,
        payload=bytes.fromhex("79fc001c"),
    )
    response = Frame(
        to_addr=0x7E,
        from_addr=0x80,
        association_id=0xEA,
        request_id=0x74,
        control=0x81,
        msg_type=0x03,
        payload=bytes.fromhex(
            "3802720296011a04f604f601f800000000000000001a00000501590000"
            "00000000000000000000000000000000000000000000000062846a39"
        ),
    )

    assert tracker.describe(request) == "READ request addr=0x79fc size=0x001c (sensors_app)"
    assert tracker.describe(response) == "SENSORS dhw=62.6°C cdc=66.2°C dhw_power=0.0kW heating_power=0.0kW pressure=1.3bar"


def test_encode_satellite_mode_variants() -> None:
    zs = ZoneState(zone=1)
    zs.mode = ZoneMode.FROST
    assert encode_satellite_mode(zs) == 0x10
    zs.mode = ZoneMode.COMFORT
    assert encode_satellite_mode(zs) == 0x01
    zs.mode = ZoneMode.REDUCED
    assert encode_satellite_mode(zs) == 0x00

    zs.mode = ZoneMode.AUTO
    zs.override = False
    zs.auto_comfort = True
    assert encode_satellite_mode(zs) == 0x05
    zs.auto_comfort = False
    assert encode_satellite_mode(zs) == 0x04
    zs.override = True
    zs.auto_comfort = True
    assert encode_satellite_mode(zs) == 0x03
    zs.auto_comfort = False
    assert encode_satellite_mode(zs) == 0x02

    # Connect-compatible encoding XORs 0x20.
    zs.mode = ZoneMode.COMFORT
    assert encode_satellite_mode(zs, connect_compat=True) == 0x21


def test_encode_satellite_mode_unknown_raises() -> None:
    zs = ZoneState(zone=2)
    with pytest.raises(ValueError, match="mode unknown"):
        encode_satellite_mode(zs)


def test_decode_zone_consigne_populates_zone() -> None:
    data = BoilerData()
    decode_zone_consigne(bytes.fromhex("a0290015a02f00040800c800be00040000"), 1, data)
    zone = data.zones[1]
    assert zone.ambient_temperature == 20.0
    assert zone.setpoint_temperature == 19.0
    assert zone.mode == ZoneMode.AUTO
    assert zone.auto_comfort is False
    assert zone.override is False


# Real captures (logs/re). The 0xa0f0 block is pushed for holiday AND unrelated
# program/manual/DHW changes; the trailing byte is a change-toggle flag.
HOLIDAY_NONE_PUSH = bytes.fromhex(
    "a0f0000d1a0000000000000000000000000000000000000000000000000088"
)
HOLIDAY_ON_PUSH = bytes.fromhex(
    "a0f0000d1acc806a39000000001e006a3b0000000000000000000000000088"
)
# max-temp experiment: clear then set, all-zero tail (no boost bit).
BOILER_EVENT_CLEAR = bytes.fromhex("020a8031ac553a6a00000000000000000000000000")
BOILER_EVENT_SET = bytes.fromhex("010a8031ad553a6a00000000000000000000000000")


def test_parse_memory_push_splits_body_and_trailer() -> None:
    push = parse_memory_push(HOLIDAY_NONE_PUSH)
    assert push.address == 0xA0F0
    assert push.length == 0x000D
    assert len(push.body) == 13
    assert push.toggle_flag == 0x88


def test_decode_holiday_push_none() -> None:
    data = BoilerData()
    push = parse_memory_push(HOLIDAY_NONE_PUSH)
    holiday = decode_holiday_push(push.body, data)
    assert holiday.active is False
    assert data.boiler.holiday_active is False


def test_decode_holiday_push_active_dates() -> None:
    data = BoilerData()
    push = parse_memory_push(HOLIDAY_ON_PUSH)
    holiday = decode_holiday_push(push.body, data)
    assert holiday.active is True
    assert data.boiler.holiday_active is True
    assert holiday.start is not None
    assert holiday.start.isoformat(timespec="seconds") == "2026-06-23T00:00:00+00:00"
    assert holiday.end is not None
    assert holiday.end.isoformat(timespec="seconds") == "2026-06-24T00:00:00+00:00"


def test_describe_memory_push_holiday_none() -> None:
    text = describe_memory_push(HOLIDAY_NONE_PUSH)
    assert text == "MEMORY_PUSH addr=holiday len=0x000d holiday=none flag=0x88"


def test_describe_memory_push_holiday_on() -> None:
    text = describe_memory_push(HOLIDAY_ON_PUSH)
    assert text == (
        "MEMORY_PUSH addr=holiday len=0x000d holiday=on "
        "start=2026-06-23T00:00:00+00:00 end=2026-06-24T00:00:00+00:00 flag=0x88"
    )


def test_parse_boiler_event_push() -> None:
    clear = parse_boiler_event_push(BOILER_EVENT_CLEAR)
    assert clear.kind == BOILER_EVENT_KIND_CLEAR
    assert clear.kind_label == "clear"
    assert clear.memory_tag.hex() == "0a8031"
    assert clear.stamp.hex() == "ac553a6a"

    settle = parse_boiler_event_push(BOILER_EVENT_SET)
    assert settle.kind == BOILER_EVENT_KIND_SET
    assert settle.kind_label == "set"


def test_describe_boiler_event() -> None:
    assert describe_boiler_event(BOILER_EVENT_CLEAR) == (
        "BOILER_EVENT kind=clear tag=0x0a8031 stamp=ac553a6a"
    )
    assert describe_boiler_event(BOILER_EVENT_SET) == (
        "BOILER_EVENT kind=set tag=0x0a8031 stamp=ad553a6a"
    )


def test_passive_read_tracker_describes_memory_push() -> None:
    tracker = PassiveReadTracker()
    frame = Frame(
        to_addr=0x7E,
        from_addr=0x80,
        association_id=0xEA,
        request_id=0xB4,
        control=0x05,
        msg_type=MSG_MEMORY_PUSH,
        payload=HOLIDAY_ON_PUSH,
    )
    assert tracker.describe(frame) == (
        "MEMORY_PUSH addr=holiday len=0x000d holiday=on "
        "start=2026-06-23T00:00:00+00:00 end=2026-06-24T00:00:00+00:00 flag=0x88"
    )


def test_passive_read_tracker_describes_boiler_event() -> None:
    tracker = PassiveReadTracker()
    frame = Frame(
        to_addr=0x7E,
        from_addr=0x80,
        association_id=0xEA,
        request_id=0xD4,
        control=0x05,
        msg_type=MSG_BOILER_EVENT,
        payload=BOILER_EVENT_CLEAR,
    )
    assert tracker.describe(frame) == "BOILER_EVENT kind=clear tag=0x0a8031 stamp=ac553a6a"


def test_decode_memory_push_updates_model() -> None:
    data = BoilerData()
    decode_memory_push(HOLIDAY_NONE_PUSH, data)
    assert data.boiler.holiday_active is False
