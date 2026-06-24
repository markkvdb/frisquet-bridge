# Host ↔ Modem Serial Protocol

Line-based protocol over USB serial at **115200 baud**. Each line is terminated with `\n` (LF). Optional `\r` before `\n` is ignored.

## Integrity

Most host commands end with a **CRC8** (XOR of all preceding ASCII bytes, formatted as two lowercase hex digits):

```
LISTEN aa
```

Commands without a CRC field: none required for boot messages from the modem.

## Modem → Host (unsolicited)

| Line | Description |
|------|-------------|
| `READY <name> <version>` | Sent once after boot and radio init |
| `RX <rssi> <hex> <crc>` | Received RF frame. `rssi` is signed dBm. `hex` is the full 7-byte Frisquet metadata + payload (see below). |
| `OK <seq>` | Command `seq` succeeded |
| `ERR <seq> <reason>` | Command failed (`bad_crc`, `bad_hex`, `tx_fail`, `unknown`, `busy`) |
| `PONG <seq>` | Response to `PING` |
| `INFO <key> <value>` | Response to `VERSION` (`INFO version 1.0.0`) |
| `HB` | Heartbeat every 30 s while in LISTEN mode |

## Host → Modem

| Command | Description |
|---------|-------------|
| `NID <hex8> <crc>` | Set 4-byte RF sync word (network ID). Use the boiler network ID for normal traffic. `ffffffff` is useful for association/pairing traffic, but it is still a real sync word, not a wildcard. |
| `TX <hex> <crc>` | Transmit a frame (see byte layout below). |
| `LISTEN <crc>` | Enter continuous RX mode (promiscuous). |
| `SLEEP <crc>` | Stop RX, radio idle. |
| `PING <seq> <crc>` | Connectivity check |
| `VERSION <crc>` | Request firmware version |

`<seq>` is a decimal integer 0–255, echoed in `OK`/`ERR`/`PONG`.

## RF frame byte layout (TX and RX `hex` field)

Same as the original frisquet-connect / RadioHead mapping:

| Offset | Field | Maps to RadioHead |
|--------|-------|-------------------|
| 0 | `length` | Total metadata tail + payload (excluding this byte): `6 + payload_len` |
| 1 | `to_addr` | `headerTo` |
| 2 | `from_addr` | `headerFrom` |
| 3 | `association_id` | `headerId` |
| 4 | `request_id` | `headerFlags` |
| 5 | `control` | First byte of RH payload |
| 6 | `msg_type` | Second byte of RH payload |
| 7+ | data | Remaining RH payload |

On **TX**, the modem sends RH payload = bytes `[5..]` with length `length - 4`.

On **RX**, the modem reconstructs the full hex line from RH headers + payload.

## Typical sniff session

```
# host sends (CRC computed by tool):
NID 05d97f78 65
LISTEN 09

# modem receives boiler traffic:
RX -48 0e807e809c18010379e0001c... ee
```

## Radio parameters (fixed)

| Parameter | Value |
|-----------|-------|
| Frequency | 868.96 MHz |
| Modulation | FSK |
| Bit rate | 25 kbps |
| Freq deviation | 50 kHz |
| RX bandwidth | ~250 kHz |
| Preamble | 4 bytes |
| Sync word | 4 bytes (from `NID`) |
| Promiscuous | Enabled in LISTEN mode |

## RF message types (Frisquet payload)

The `msg_type` byte (offset 6) selects the operation. Besides the documented
`READ` (`0x03`) and `INIT` (`0x17`) exchanges, the boiler may **push** updates
to the Connect using:

| `msg_type` | Name | Direction | `control` | Payload |
|------------|------|-----------|-----------|---------|
| `0x10` | `MEMORY_PUSH` | boiler → connect | `0x05` | `addr u16 BE \| len u16 BE \| body` |
| `0x45` | `BOILER_EVENT` | boiler → connect | `0x05` | 21 bytes (see FRISQUET.md §6.6) |

Connect ACK of either push: swapped addresses, same `request_id`, `control=0x85`,
same `msg_type`, payload often empty (memory push ACK may echo `addr+len` only).

Detailed field semantics and capture methodology: [`FRISQUET.md`](./FRISQUET.md) §6.6.
