"""Persist learned zone metadata (schedule + setpoints) across restarts.

The boiler never serves zone config to Connect, and the satellite only emits
its 0xa154 block when a setting changes (not periodically). So once we have
sniffed a zone's schedule/setpoints we save them to disk; a single capture then
survives restarts and lets zone writes proceed without waiting to re-learn.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from frisquet_bridge.model import SCHEDULE_DAYS, BoilerData, ZoneMode, ZoneSchedule, ZoneState

log = structlog.get_logger(__name__)
PROTOCOL_KEY = "protocol"


def _load_payload(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("state_load_failed", path=str(path), error=str(exc))
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_payload(path: Path, payload: dict[str, object]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _serialize_zone(zone: ZoneState) -> dict[str, object] | None:
    zone_temperatures = (
        zone.comfort_temperature,
        zone.reduced_temperature,
        zone.frost_temperature,
    )
    central_values = (
        zone.central_setpoint,
        zone.central_demand,
        zone.central_demand_on_delta,
        zone.central_demand_off_margin,
    )
    if (
        zone.schedule is None
        and all(value is None for value in zone_temperatures)
        and all(value is None for value in central_values)
    ):
        return None
    schedule_hex: str | None = None
    if zone.schedule is not None:
        schedule_hex = b"".join(zone.schedule.days[day] for day in SCHEDULE_DAYS).hex()
    return {
        "mode": zone.mode.value if zone.mode is not None else None,
        "mode_options": zone.mode_options,
        "comfort_temperature": zone.comfort_temperature,
        "reduced_temperature": zone.reduced_temperature,
        "frost_temperature": zone.frost_temperature,
        "schedule": schedule_hex,
        "central_setpoint": zone.central_setpoint,
        "central_demand": zone.central_demand,
        "central_demand_on_delta": zone.central_demand_on_delta,
        "central_demand_off_margin": zone.central_demand_off_margin,
    }


def _apply_zone(zone: ZoneState, raw: dict[str, object]) -> None:
    mode = raw.get("mode")
    if isinstance(mode, str):
        zone.mode = ZoneMode.parse(mode)
    if isinstance(raw.get("mode_options"), int):
        zone.mode_options = int(raw["mode_options"])
    for attr in ("comfort_temperature", "reduced_temperature", "frost_temperature"):
        value = raw.get(attr)
        if isinstance(value, int | float) and not isinstance(value, bool):
            setattr(zone, attr, float(value))
    central_demand = raw.get("central_demand")
    if isinstance(central_demand, bool):
        zone.central_demand = central_demand
    for attr in ("central_setpoint", "central_demand_on_delta", "central_demand_off_margin"):
        value = raw.get(attr)
        if isinstance(value, int | float) and not isinstance(value, bool):
            setattr(zone, attr, float(value))
    schedule = raw.get("schedule")
    if isinstance(schedule, str):
        zone.schedule = ZoneSchedule.decode(bytes.fromhex(schedule))


def save_zone_state(path: Path, data: BoilerData) -> None:
    """Atomically persist any learned zone metadata to ``path``."""
    try:
        payload = _load_payload(path)
        for n, zs in data.zones.items():
            zone = _serialize_zone(zs)
            if zone is None:
                payload.pop(str(n), None)
            else:
                payload[str(n)] = zone
        _write_payload(path, payload)
    except OSError as exc:
        log.warning("zone_state_save_failed", path=str(path), error=str(exc))


def load_zone_state(path: Path, data: BoilerData) -> None:
    """Populate ``data`` with previously-learned zone metadata, if present."""
    payload = _load_payload(path)
    loaded = []
    for n, zone in data.zones.items():
        raw = payload.get(str(n))
        if isinstance(raw, dict):
            try:
                _apply_zone(zone, raw)
                loaded.append(n)
            except (ValueError, KeyError) as exc:
                log.warning("zone_state_apply_failed", zone=n, error=str(exc))
    if loaded:
        log.info("zone_state_loaded", zones=loaded, path=str(path))


def load_protocol_request_ids(path: Path) -> dict[str, int]:
    payload = _load_payload(path)
    raw = payload.get(PROTOCOL_KEY)
    if not isinstance(raw, dict):
        return {}
    request_ids: dict[str, int] = {}
    for role, state in raw.items():
        if not isinstance(role, str) or not isinstance(state, dict):
            continue
        request_id = state.get("request_id")
        if isinstance(request_id, int) and not isinstance(request_id, bool) and 0 <= request_id <= 0xFF:
            request_ids[role] = request_id
    return request_ids


def save_protocol_request_id(path: Path, role: str, request_id: int) -> None:
    try:
        payload = _load_payload(path)
        protocol = payload.setdefault(PROTOCOL_KEY, {})
        if not isinstance(protocol, dict):
            protocol = {}
            payload[PROTOCOL_KEY] = protocol
        protocol[role] = {"request_id": request_id & 0xFF}
        _write_payload(path, payload)
    except OSError as exc:
        log.warning("protocol_state_save_failed", path=str(path), role=role, error=str(exc))
