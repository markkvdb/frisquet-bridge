"""High-level protocol client: request/response loop, reads, writes, pairing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog

from frisquet_bridge.connect.state import ProtocolState
from frisquet_bridge.frame import (
    ADDR_BOILER,
    ADDR_BROADCAST,
    ADDR_CONNECT,
    MSG_ASSOCIATION,
    MSG_READ,
    Frame,
)
from frisquet_bridge.transport.base import Transport, TransportError

log = structlog.get_logger(__name__)

# Frisquet Connect firmware version advertised during pairing.
CONNECT_VERSION = bytes((0x01, 0x21, 0x01, 0x02))

_DEFAULT_TIMEOUT = 5.0
_DEFAULT_RETRIES = 3


@dataclass(frozen=True, slots=True)
class Association:
    network_id: bytes
    association_id: int
    request_id: int


class FrisquetClient:
    """Drives request/response exchanges with the boiler over a Transport."""

    def __init__(
        self,
        transport: Transport,
        state: ProtocolState,
        *,
        self_addr: int = ADDR_CONNECT,
        boiler_addr: int = ADDR_BOILER,
        lock: asyncio.Lock | None = None,
    ) -> None:
        self._transport = transport
        self._state = state
        self._self_addr = self_addr
        self._boiler_addr = boiler_addr
        # Serialises request/response exchanges so multiple clients sharing one
        # modem (connect + sonde + satellites) don't transmit over each other.
        self._lock = lock or asyncio.Lock()

    @property
    def state(self) -> ProtocolState:
        return self._state

    async def request(
        self,
        *,
        control: int,
        msg_type: int,
        payload: bytes,
        from_addr: int | None = None,
        to_addr: int | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        retries: int = _DEFAULT_RETRIES,
    ) -> Frame:
        """Send a frame and await the boiler's matching response.

        The response is identified by swapped addresses and the same
        association id / request id (as in frisquet-connect).
        """
        from_addr = self._self_addr if from_addr is None else from_addr
        to_addr = self._boiler_addr if to_addr is None else to_addr

        async with self._lock:
            return await self._request_locked(
                control=control,
                msg_type=msg_type,
                payload=payload,
                from_addr=from_addr,
                to_addr=to_addr,
                timeout=timeout,
                retries=retries,
            )

    async def _request_locked(
        self,
        *,
        control: int,
        msg_type: int,
        payload: bytes,
        from_addr: int,
        to_addr: int,
        timeout: float,
        retries: int,
    ) -> Frame:
        await self._transport.set_network_id(self._state.network_id)

        async with self._transport.subscribe() as queue:
            last_error: Exception | None = None
            for attempt in range(1, retries + 1):
                req_id = self._state.next_request_id()
                frame = Frame(
                    to_addr=to_addr,
                    from_addr=from_addr,
                    association_id=self._state.association_id,
                    request_id=req_id,
                    control=control,
                    msg_type=msg_type,
                    payload=payload,
                )
                # Drain any frames buffered before this attempt.
                _drain(queue)
                try:
                    await self._transport.send(frame)
                except TransportError as exc:
                    last_error = exc
                    log.warning(
                        "request_send_failed",
                        attempt=attempt,
                        retries=retries,
                        to_addr=f"0x{to_addr:02x}",
                        from_addr=f"0x{from_addr:02x}",
                        association_id=f"0x{self._state.association_id:02x}",
                        request_id=f"0x{req_id:02x}",
                        msg_type=f"0x{msg_type:02x}",
                        error=str(exc),
                    )
                    continue

                response = await self._await_match(
                    queue,
                    from_addr=to_addr,
                    to_addr=from_addr,
                    association_id=self._state.association_id,
                    request_id=req_id,
                    msg_type=msg_type,
                    timeout=timeout,
                )
                if response is not None:
                    log.debug(
                        "request_succeeded",
                        attempt=attempt,
                        to_addr=f"0x{to_addr:02x}",
                        from_addr=f"0x{from_addr:02x}",
                        association_id=f"0x{self._state.association_id:02x}",
                        request_id=f"0x{req_id:02x}",
                        msg_type=f"0x{msg_type:02x}",
                    )
                    return response
                last_error = TransportError("no matching response")
                log.warning(
                    "request_no_response",
                    attempt=attempt,
                    retries=retries,
                    to_addr=f"0x{to_addr:02x}",
                    from_addr=f"0x{from_addr:02x}",
                    association_id=f"0x{self._state.association_id:02x}",
                    request_id=f"0x{req_id:02x}",
                    msg_type=f"0x{msg_type:02x}",
                )

            raise TransportError(f"request failed after {retries} attempts: {last_error}")

    async def send_oneway(
        self,
        *,
        control: int,
        msg_type: int,
        payload: bytes,
        from_addr: int | None = None,
        to_addr: int | None = None,
        repeats: int = 1,
        interval: float = 0.0,
    ) -> None:
        """Transmit a frame without awaiting a response (fire-and-forget).

        Relayed zone writes (``control`` = a satellite/zone id) are forwarded by
        the boiler to the physical satellite and are *never* acknowledged
        directly to Connect, so there is no matching response to await. The
        official app simply (re)sends the frame a few times; we mirror that with
        ``repeats``/``interval`` for reliability over the lossy RF link.
        """
        from_addr = self._self_addr if from_addr is None else from_addr
        to_addr = self._boiler_addr if to_addr is None else to_addr

        for index in range(repeats):
            # Take the lock per send (not across the whole burst) so the gaps
            # between retransmits stay free for the sonde / polling reads that
            # share this modem.
            async with self._lock:
                await self._transport.set_network_id(self._state.network_id)
                req_id = self._state.next_request_id()
                frame = Frame(
                    to_addr=to_addr,
                    from_addr=from_addr,
                    association_id=self._state.association_id,
                    request_id=req_id,
                    control=control,
                    msg_type=msg_type,
                    payload=payload,
                )
                await self._transport.send(frame)
            if interval > 0 and index + 1 < repeats:
                await asyncio.sleep(interval)

    async def read_memory(self, address: int, size: int, **kwargs: object) -> bytes:
        """READ ``size`` bytes at ``address``; returns the response payload."""
        payload = address.to_bytes(2, "big") + size.to_bytes(2, "big")
        response = await self.request(
            control=0x01,
            msg_type=MSG_READ,
            payload=payload,
            **kwargs,  # type: ignore[arg-type]
        )
        return response.payload

    async def _await_match(
        self,
        queue: asyncio.Queue,
        *,
        from_addr: int,
        to_addr: int,
        association_id: int,
        request_id: int,
        msg_type: int,
        timeout: float,
    ) -> Frame | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                received = await asyncio.wait_for(queue.get(), remaining)
            except TimeoutError:
                return None
            if received.frame.matches(
                from_addr=from_addr,
                to_addr=to_addr,
                association_id=association_id,
                request_id=request_id,
            ) and received.frame.msg_type == msg_type and received.frame.is_ack:
                return received.frame

    async def recover_network_id(self, *, timeout: float = 30.0) -> bytes:
        """Sniff the boiler's association broadcast and return its network id.

        Unlike :meth:`pair`, this never transmits a reply, so the boiler's
        association stays open. Trigger an association (or "replace satellite")
        on the boiler, then cancel it once the id has been captured.
        """
        await self._transport.set_network_id(b"\xff\xff\xff\xff")
        log.info("network_id_recovery_waiting", timeout=timeout)
        async with self._transport.subscribe() as queue:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TransportError("network id recovery timed out (no broadcast)")
                try:
                    received = await asyncio.wait_for(queue.get(), remaining)
                except TimeoutError:
                    raise TransportError("network id recovery timed out (no broadcast)") from None
                frame = received.frame
                if frame.msg_type != MSG_ASSOCIATION or frame.control != 0x02:
                    continue
                if frame.to_addr != ADDR_BROADCAST or len(frame.payload) < 5:
                    continue
                network_id = frame.payload[1:5]
                log.info("network_id_recovered", network_id=network_id.hex())
                return network_id

    async def pair(
        self,
        from_addr: int,
        *,
        listen_window: float = 5.0,
    ) -> Association:
        """Listen for the boiler's association broadcast and reply.

        Set the boiler into association mode first. Replies to every broadcast
        until the boiler stops sending (one quiet ``listen_window``).
        """
        await self._transport.set_network_id(b"\xff\xff\xff\xff")
        log.info("pairing_waiting", from_addr=f"0x{from_addr:02x}", listen_window=listen_window)

        result: Association | None = None
        async with self._transport.subscribe() as queue:
            while True:
                try:
                    received = await asyncio.wait_for(queue.get(), listen_window)
                except TimeoutError:
                    if result is not None:
                        return result
                    raise TransportError("pairing timed out (no broadcast)") from None

                frame = received.frame
                if frame.msg_type != MSG_ASSOCIATION or frame.control != 0x02:
                    continue
                if frame.to_addr != ADDR_BROADCAST:
                    continue
                if len(frame.payload) < 5:
                    continue

                network_id = frame.payload[1:5]
                result = Association(
                    network_id=network_id,
                    association_id=frame.association_id,
                    request_id=frame.request_id,
                )
                log.info(
                    "pairing_broadcast_received",
                    from_addr=f"0x{from_addr:02x}",
                    network_id=network_id.hex(),
                    association_id=f"0x{frame.association_id:02x}",
                    request_id=f"0x{frame.request_id:02x}",
                )
                reply = Frame(
                    to_addr=ADDR_BOILER,
                    from_addr=from_addr,
                    association_id=frame.association_id,
                    request_id=frame.request_id,
                    control=frame.control + 0x80,
                    msg_type=MSG_ASSOCIATION,
                    payload=CONNECT_VERSION,
                )
                await self._transport.send(reply)


def _drain(queue: asyncio.Queue) -> None:
    while not queue.empty():
        queue.get_nowait()
