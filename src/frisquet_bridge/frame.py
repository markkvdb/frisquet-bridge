"""Frisquet RF frame model: the 7-byte header + payload.

Wire layout (matches frisquet-connect / RadioHead mapping):

    offset 0: length        = len(payload) + 6
    offset 1: to_addr        -> RadioHead headerTo
    offset 2: from_addr      -> RadioHead headerFrom
    offset 3: association_id -> RadioHead headerId
    offset 4: request_id     -> RadioHead headerFlags
    offset 5: control        | first bytes of the RadioHead payload
    offset 6: msg_type       |
    offset 7+: payload

The modem reconstructs this exact byte string on RX and consumes it on TX.
"""

from __future__ import annotations

from dataclasses import dataclass, field

HEADER_LEN = 7

# Frisquet device addresses.
ADDR_BROADCAST = 0x00
ADDR_SATELLITE_Z1 = 0x08
ADDR_SATELLITE_Z2 = 0x09
ADDR_SATELLITE_Z3 = 0x0A
ADDR_SONDE = 0x20
ADDR_CONNECT = 0x7E
ADDR_BOILER = 0x80
ADDR_BOILER_ALT = 0x84

# Message types (the msg_type byte).
MSG_READ = 0x03
MSG_MEMORY_PUSH = 0x10
MSG_INIT = 0x17
MSG_BOILER_EVENT = 0x45
MSG_ASSOCIATION = 0x41
MSG_SONDE_INIT = 0x43

MSG_TYPE_NAMES: dict[int, str] = {
    MSG_READ: "READ",
    MSG_MEMORY_PUSH: "MEMORY_PUSH",
    MSG_INIT: "INIT",
    MSG_BOILER_EVENT: "BOILER_EVENT",
    MSG_ASSOCIATION: "ASSOCIATION",
    MSG_SONDE_INIT: "SONDE_INIT",
}

DEVICE_NAMES: dict[int, str] = {
    ADDR_BROADCAST: "broadcast",
    ADDR_SATELLITE_Z1: "satellite_z1",
    ADDR_SATELLITE_Z2: "satellite_z2",
    ADDR_SATELLITE_Z3: "satellite_z3",
    ADDR_SONDE: "sonde_ext",
    ADDR_CONNECT: "connect",
    ADDR_BOILER: "boiler",
    ADDR_BOILER_ALT: "boiler_alt",
}

ACK_FLAG = 0x80


class FrameError(ValueError):
    """Raised when a byte buffer cannot be decoded into a Frame."""


@dataclass(frozen=True, slots=True)
class Frame:
    """A decoded Frisquet RF frame."""

    to_addr: int
    from_addr: int
    association_id: int
    request_id: int
    control: int
    msg_type: int
    payload: bytes = field(default=b"")

    def encode(self) -> bytes:
        length = len(self.payload) + 6
        if length > 0xFF:
            raise FrameError(f"payload too long: {len(self.payload)} bytes")
        return (
            bytes(
                (
                    length,
                    self.to_addr,
                    self.from_addr,
                    self.association_id,
                    self.request_id,
                    self.control,
                    self.msg_type,
                )
            )
            + self.payload
        )

    @classmethod
    def decode(cls, raw: bytes) -> Frame:
        if len(raw) < HEADER_LEN:
            raise FrameError(f"frame too short: {len(raw)} bytes")
        length, to_addr, from_addr, assoc, req_id, control, msg_type = raw[:HEADER_LEN]
        # length counts everything after the length byte itself.
        expected = length + 1
        if len(raw) < expected:
            raise FrameError(f"truncated frame: length={length} implies {expected} bytes, got {len(raw)}")
        return cls(
            to_addr=to_addr,
            from_addr=from_addr,
            association_id=assoc,
            request_id=req_id,
            control=control,
            msg_type=msg_type,
            payload=bytes(raw[HEADER_LEN:expected]),
        )

    @property
    def is_ack(self) -> bool:
        return bool(self.control & ACK_FLAG)

    @property
    def payload_hex(self) -> str:
        return self.payload.hex()

    def matches(
        self,
        *,
        from_addr: int,
        to_addr: int,
        association_id: int,
        request_id: int,
    ) -> bool:
        """True if this frame is the response identified by these fields."""
        return (
            self.from_addr == from_addr
            and self.to_addr == to_addr
            and self.association_id == association_id
            and self.request_id == request_id
        )

    def describe(self) -> str:
        mt = MSG_TYPE_NAMES.get(self.msg_type, f"0x{self.msg_type:02x}")
        ack = " ACK" if self.is_ack else ""
        parts = [
            f"{device_name(self.from_addr)} -> {device_name(self.to_addr)}",
            f"type={mt}",
            f"assoc=0x{self.association_id:02x}",
            f"req=0x{self.request_id:02x}",
            f"ctrl=0x{self.control:02x}{ack}",
        ]
        if self.payload:
            parts.append(f"data={self.payload_hex}")
        return " | ".join(parts)


def device_name(addr: int) -> str:
    name = DEVICE_NAMES.get(addr)
    return f"{name}({addr:#04x})" if name else f"0x{addr:02x}"
