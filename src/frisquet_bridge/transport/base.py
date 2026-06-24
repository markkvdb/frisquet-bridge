"""Transport abstraction for talking to the RF modem.

A Transport is responsible only for moving RF frames to/from the modem and
setting the network id / listen state. It has no knowledge of the Frisquet
protocol semantics. This keeps the modem link swappable (serial today; a
different link later) without touching the protocol layer.
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from frisquet_bridge.frame import Frame


class TransportError(Exception):
    """Raised on modem communication failures (NACK, timeout, I/O)."""


@dataclass(frozen=True, slots=True)
class ReceivedFrame:
    """An RF frame received by the modem, with signal strength."""

    rssi: int
    frame: Frame
    raw: bytes


class Transport(abc.ABC):
    """Async modem transport."""

    async def __aenter__(self) -> Transport:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @abc.abstractmethod
    async def open(self) -> None:
        """Open the underlying link and start reading."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Stop reading and release the link."""

    @abc.abstractmethod
    async def set_network_id(self, network_id: bytes) -> None:
        """Set the 4-byte RF sync word."""

    @abc.abstractmethod
    async def send(self, frame: Frame) -> None:
        """Transmit a frame (waits for the modem's OK/ERR)."""

    @abc.abstractmethod
    async def listen(self) -> None:
        """Put the modem into continuous receive mode."""

    @abc.abstractmethod
    async def sleep(self) -> None:
        """Put the modem radio idle."""

    @abc.abstractmethod
    def subscribe(self) -> AbstractAsyncContextManager[asyncio.Queue[ReceivedFrame]]:
        """Register a queue receiving every RF frame for the block's duration.

        Subscribing before sending avoids the race where a fast response
        arrives before the consumer starts iterating.
        """

    async def frames(self) -> AsyncIterator[ReceivedFrame]:
        """Yield received frames as they arrive."""
        async with self.subscribe() as queue:
            while True:
                yield await queue.get()
