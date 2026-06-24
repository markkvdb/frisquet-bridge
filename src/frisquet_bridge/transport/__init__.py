"""Modem transport layer (serial today, swappable later)."""

from frisquet_bridge.transport.base import ReceivedFrame, Transport, TransportError

__all__ = ["ReceivedFrame", "Transport", "TransportError"]
