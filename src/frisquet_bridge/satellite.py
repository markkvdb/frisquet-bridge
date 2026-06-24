"""Virtual satellite emulation: act as a zone thermostat toward the boiler.

When a zone is configured as ``virtual_satellite`` the bridge owns that zone's
thermostat role. It periodically reports the room (ambient) temperature plus the
active setpoint and mode to the boiler at 0xa02f, exactly as a physical Frisquet
satellite would (see OpenFrisquetVisio ``Satellite::envoyerConsigne``). The
ambient temperature is fed from Home Assistant; the setpoint/mode come from the
HA climate entity.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import structlog

from frisquet_bridge.boiler_profile import central_boiler_consigne, simple_consigne
from frisquet_bridge.climate import zone_target_temperature
from frisquet_bridge.connect.ops import BoilerOps
from frisquet_bridge.model import BoilerData

log = structlog.get_logger(__name__)

# OpenFrisquetVisio resends a zone's setpoint on change (debounced) and at least
# every 10 minutes so the boiler keeps the zone alive.
CONSIGNE_INTERVAL = 600.0


class VirtualSatellite:
    """Periodically reports a zone's ambient/setpoint/mode to the boiler."""

    def __init__(
        self,
        zone: int,
        ops: BoilerOps,
        data: BoilerData,
        *,
        interval: float = CONSIGNE_INTERVAL,
        connect_compat: bool = False,
        profile: str = "virtual_satellite",
        on_update: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._zone = zone
        self._ops = ops
        self._data = data
        self._interval = interval
        self._connect_compat = connect_compat
        self._profile = profile
        self._on_update = on_update
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()

    def request_send(self) -> None:
        """Ask the runner to send a consigne now (e.g. after an HA command)."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            await self._send_once()
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)

    async def send_now(self) -> bool:
        """Send a single consigne immediately; returns True if it was sent."""
        return await self._send_once()

    async def _send_once(self) -> bool:
        zs = self._data.zones[self._zone]
        if self._profile == "central_boiler":
            consigne = central_boiler_consigne(zs)
            ambient = consigne.ambient if consigne is not None else None
            setpoint = consigne.setpoint if consigne is not None else None
        elif self._profile == "simple_satellite":
            consigne = simple_consigne(zs)
            ambient = consigne.ambient if consigne is not None else None
            setpoint = consigne.setpoint if consigne is not None else None
        else:
            ambient = zs.reported_ambient if zs.reported_ambient is not None else zs.ambient_temperature
            setpoint = zone_target_temperature(zs)
        if ambient is None or zs.mode is None or setpoint is None:
            log.debug(
                "virtual_satellite_skip",
                zone=self._zone,
                profile=self._profile,
                ambient=ambient,
                setpoint=setpoint,
                mode=zs.mode.value if zs.mode is not None else None,
            )
            return False
        try:
            await self._ops.send_zone_consigne(
                self._zone,
                self._data,
                ambient=ambient,
                setpoint=setpoint,
                connect_compat=self._connect_compat,
            )
        except Exception:
            log.exception("virtual_satellite_send_failed", zone=self._zone)
            return False
        if self._on_update is not None:
            await self._on_update()
        return True
