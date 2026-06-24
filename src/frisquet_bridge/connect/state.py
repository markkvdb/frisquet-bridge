"""Mutable RF protocol state (network id, association id, rolling request id).

Persistence is handled separately (config layer); an optional ``on_change``
hook lets the owner save state after the request id advances.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class ProtocolState:
    network_id: bytes
    association_id: int
    request_id: int = 0
    on_change: Callable[[ProtocolState], None] | None = field(default=None, repr=False, compare=False)

    def next_request_id(self) -> int:
        # Matches frisquet-connect: increment by 4, wrapping at 256.
        self.request_id = (self.request_id + 4) & 0xFF
        if self.on_change is not None:
            self.on_change(self)
        return self.request_id
