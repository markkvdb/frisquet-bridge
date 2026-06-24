# Frisquet Devices & Radio Logic

How a Frisquet *Eco Radio System Visio* heating installation is structured, who
talks to whom over the 868 MHz radio, and the timing rules that govern whether a
message is actually accepted. This is a behavioural description of the **devices
and the high-level RF logic** — the byte-level host↔modem framing lives in
[`PROTOCOL.md`](./PROTOCOL.md).

Everything here was confirmed by sniffing a live installation and the official
Frisquet Connect app, cross-checked against the OpenFrisquetVisio project. Where
a detail is inferred rather than observed it is called out explicitly.

---

## 1. The devices

A Frisquet installation is a small star network of single-purpose radios that
all share **one half-duplex channel** and a common 4-byte network id (RF sync
word). Every device has a fixed 1-byte address.

| Address | Device | Role |
|--------:|--------|------|
| `0x80` | **Boiler** (chaudière) | The hub and source of truth for boiler sensors. Relays/answers all other devices. Some installs use `0x84`. |
| `0x7E` | **Connect** | Cloud gateway. Lets the phone app read state and push commands. Polls the boiler and forwards zone commands. |
| `0x08` | **Satellite zone 1** | The wall thermostat for a heating zone: shows/holds the mode, setpoints and weekly program, and measures room temperature. |
| `0x09` | **Satellite zone 2** | Same, zone 2. |
| `0x0A` | **Satellite zone 3** | Same, zone 3. |
| `0x20` | **External probe** (sonde) | Outside-temperature sensor that pushes its reading to the boiler. |
| `0x00` | Broadcast | Used during association/pairing. |

Notes:

- A zone is driven by **exactly one** controller: either a physical satellite,
  the Connect, or (in this bridge) a *virtual satellite*. Two controllers on the
  same zone address will transmit over each other.
- The **boiler** measures all boiler-side values (DHW temperature, pressure,
  flue, power, consumption, status, …). It does **not** measure room
  temperature — that comes from the per-zone satellite.
- The **boiler does not persist a "desired zone state" pushed by the Connect**.
  It holds the *current* zone setpoint/mode (what it last received from the
  satellite) and relays Connect writes onward. This is the single most important
  fact for understanding why commands sometimes "don't take" (see §5).

### Where the bridge fits

This project replaces the Connect (`0x7E`) for reads and can additionally
**emulate** devices it is configured for:

- a **virtual external probe** (`0x20`) to push an outside temperature, and
- a **virtual satellite** (`0x08/0x09/0x0A`) to fully own a zone's thermostat
  role — the reliable way to control a zone (see §8).

---

## 2. Frame essentials

Every RF frame carries a 7-byte header (see `PROTOCOL.md` for the wire layout):

| Field | Meaning |
|-------|---------|
| `to_addr` / `from_addr` | Destination / source device address |
| `association_id` | Identifies the logical link a device has with the boiler. Each device has its own (e.g. Connect `0xEA`, satellite z1 `0xC0`, sonde varies). |
| `request_id` | Rolling id, **incremented by 4** each request, wrapping at 256. Echoed back in the matching response. |
| `control` | Direction/relay/ACK semantics (see §3). |
| `msg_type` | Operation class (see below). |
| payload | Operation-specific bytes. |

**Message types** (`msg_type`):

| Value | Name | Used for |
|------:|------|----------|
| `0x03` | `READ` | Connect/sonde reading a boiler memory block. |
| `0x10` | `MEMORY_PUSH` | Boiler pushing an updated memory block to the Connect (unsolicited). |
| `0x17` | `INIT` | The workhorse: zone writes, satellite consignes, satellite check-ins. |
| `0x45` | `BOILER_EVENT` | Boiler pushing a compact configuration/event notification to the Connect. |
| `0x41` | `ASSOCIATION` | Pairing a new device. |
| `0x43` | `SONDE_INIT` | External-probe handshake. |

**Request/response matching.** A response is the frame with **swapped
`to`/`from`**, the **same** `association_id` and `request_id`, the same
`msg_type`, and the ACK bit set in `control`. The `0x80` bit of `control` marks
an acknowledgement (e.g. a request `control=0x01` is answered with `0x81`).

---

## 3. The `control` byte

`control` is what distinguishes a *direct exchange* from a *relayed* one. The
high bit (`0x80`) is the ACK flag; the low bits encode routing:

| `control` | Meaning |
|----------:|---------|
| `0x05` | Boiler-initiated push to the Connect (memory update or event). |
| `0x85` | Connect ACK of a boiler push (often empty payload). |
| `0x01` | Direct request to the boiler (reads, sonde/satellite consigne). |
| `0x81` | Direct ACK from the boiler. |
| `0x08` | A Connect→boiler write **addressed to zone 1's satellite** (`0x09`/`0x0A` for z2/z3). The boiler will relay it. |
| `0x7E` | The boiler **relaying** a Connect write onward to the satellite (the byte carries the original sender, `0x7E` = Connect). |
| `0xFE` | A satellite **acknowledging a relayed write** back to the boiler. |
| `0x88` | The boiler forwarding the satellite's acknowledgement back to the Connect. |

So a zone write fans out as: `Connect --0x08--> boiler --0x7E--> satellite`, and
the confirmation comes back `satellite --0xFE--> boiler --0x88--> Connect`.

---

## 4. Memory-block addressing

`READ` and the zone `INIT` write address **boiler memory blocks** by a 16-bit
address + length. Blocks seen in practice:

| Block | Meaning |
|-------|---------|
| `0xa154` | Zone configuration: comfort/reduced/frost setpoints, mode, options, weekly schedule (zone 1; zones 2/3 are offset). |
| `0xa029` | Satellite/boiler state block returned to a satellite (outside temp, date, boiler status, per-zone setpoints). |
| `0xa02f` | A satellite's setpoint **consigne** write (ambient + active setpoint + mode). |
| `0xa0f0` | Holiday / vacances schedule (abbreviated on `MEMORY_PUSH`; full block on Connect `INIT` write). |
| `0xa0fc` | DHW (ECS) mode. |
| `0x79fc`, … | Boiler sensor / consumption / clock blocks read by the Connect. |

(On boilers answering at `0x84`, block addresses are offset by `0xC8`.)

---

## 5. The golden rule: satellites sleep

**A physical satellite is battery-friendly and spends almost all of its time
asleep.** It wakes only periodically — observed about every 10 minutes at steady
state, and more frequently (every couple of minutes) while a zone is being
actively changed — to:

1. transmit its check-in (current room temperature + active setpoint + mode), and
2. briefly listen for the boiler's reply and for any relayed command.

Consequences for the radio logic:

- **A relayed command only "lands" if it arrives during a wake window.** The
  boiler relays a Connect write *immediately* and does not buffer it. If the
  satellite is asleep, the relay is simply lost; the boiler keeps reporting the
  old state on the satellite's next check-in.
- The official app copes with this by **re-sending the same write repeatedly**
  (bursts ~1.5 s apart, over a minute or more) until one relay coincides with a
  wake window. When it lands, the satellite replies `control=0xFE` and adopts
  the change.
- Pressing the satellite's physical **info button wakes it**, which makes a
  pending command land immediately — a useful diagnostic.

This is why directly controlling a physical satellite is inherently unreliable,
and why a **virtual satellite** (§8) — which is always awake and *is* the
authority — is the robust approach.

---

## 6. Message workflows

### 6.1 Connect reads boiler state (reliable)

```
Connect --READ ctrl=0x01--> boiler      (block addr + length)
Connect <--READ ctrl=0x81 ACK-- boiler  (block contents)
```

Used for boiler sensors, DHW mode, consumption, clock, and the `0xa029`
satellite/boiler state block (which carries per-zone mode/ambient/setpoint).
There is **no** "read zone configuration" exchange the boiler answers directly —
the zone schedule and comfort/reduced/frost setpoints can only be learned
**passively** by sniffing a satellite's `0xa154` broadcast.

### 6.2 Connect writes a zone (unreliable; relayed)

```
Connect --INIT ctrl=0x08--> boiler            (0xa154 zone config)
boiler  --INIT ctrl=0x7E--> satellite         (relayed; lost if asleep)
   ... only if the satellite is awake: ...
satellite --INIT ctrl=0xFE ACK--> boiler       (adopts the config)
boiler    --INIT ctrl=0x88 ACK--> Connect      (confirmation)
```

The boiler never ACKs the Connect directly for a zone write, so it is
fire-and-forget; reliability comes only from re-sending until the `0xFE` ACK
appears (see §5).

### 6.3 Satellite check-in & consigne (reliable; direct)

When a satellite wakes it reports to the boiler and is answered directly:

```
satellite --INIT ctrl=0x01--> boiler     (0xa029 read + 0xa02f write:
                                           ambient, active setpoint, mode)
satellite <--INIT ctrl=0x81 ACK-- boiler (0x2a01 state block: outside temp,
                                           date, boiler status, zone setpoints)
```

The **consigne** (`0xa02f`) is the satellite telling the boiler the room
temperature and the setpoint to hold *right now*. The boiler uses this to drive
heating and answers immediately — a true request/response, unlike §6.2. This is
the channel a virtual satellite uses.

### 6.4 External probe pushes outside temperature

```
sonde --SONDE_INIT/INIT ctrl=0x01--> boiler   (encoded outside temperature)
sonde <-- ACK -- boiler
```

### 6.5 Association / pairing

New devices are paired on the broadcast/`0xffffffff` network using
`ASSOCIATION` (`0x41`) exchanges, after which they use the boiler's real network
id and their assigned `association_id`. Pairing is out of scope here; the bridge
reuses already-established identities.

### 6.6 Boiler pushes to the Connect (event-driven)

When a setting is changed **on the boiler panel**, the boiler notifies the
Connect without waiting for a poll. These are **unsolicited** frames:
`boiler --ctrl=0x05--> connect`, answered `connect --ctrl=0x85 ACK--> boiler`.

Observed behaviour from long captures:

- **`MEMORY_PUSH` (`0x10`)** carries a memory block using the same
  `addr(2) | len(2) | body` layout as a `READ` response. The Connect ACK echoes
  only `addr+len` (4 bytes).

  Block **`0xa0f0`** is the **holiday / derogation ("vacances")** block, pushed
  with a 13-byte body: `marker(1)=0x1a | start(4) | mid(4) | end(4)`. `start` and
  `end` are 4-byte **Frisquet timestamps** (byte order `2,3,0,1`, seconds since
  the Unix epoch). When no holiday is scheduled the date fields are all zero.

  > Important: the boiler **also** pushes `0xa0f0` with an empty (all-zero) date
  > body when an *unrelated* program / manual-mode / DHW change is committed — so
  > a zero body means "no holiday dates", **not** a holiday-off command. The byte
  > right after the length-delimited body is a **change-toggle flag** whose `0x80`
  > bit flips on each commit (low nibble has been `0x08`); it is not holiday state.
  > Captured: `logs/re/holiday-on.raw.jsonl` (dates), `manual-mode` and
  > `ecs-stop-night-77-23-4` (zero body, toggling flag).

  DHW/ECS mode (block `0xa0fc`) was **not** observed as a `0x10` push on this
  installation — cycling ECS produced only zero-body `0xa0f0` pushes. The ECS
  options on this boiler also differ from the Frisquet Connect app's set, so the
  `DhwMode` enum decode is treated as model-specific / unverified here.

- **`BOILER_EVENT` (`0x45`)** is a fixed **21-byte** notification tied to the
  configuration area tagged **`0x0a8031`** (the same tag embedded in the
  `0x7a18` daily-consumption block the Connect reads every ~20 minutes). Layout:

  ```
  [kind u8][tag 0x0a8031][stamp 4B][tail 13B]
  ```

  `kind` **`0x02`** = clear, **`0x01`** = set; the boiler emits a `clear→set`
  pair. In the isolated `logs/re/max-temp.raw.jsonl` capture this pair was the
  **only** push produced by changing the **maximum temperature**, so `0x45`
  correlates with a max-temperature / boiler-parameter change. The 4-byte
  `stamp` increments between the pair (low byte `ac→ad`) but is **not** a plain
  timestamp (it decodes to year 2001 under the holiday byte order). The changed
  value has not yet been localized — `tail` was all-zero in the clean capture —
  so the payload beyond `kind` is currently treated as opaque. (An earlier mixed
  capture showed a `0x40` bit in the tail; that is **unconfirmed** as boost.)
  These pushes are **event-driven only** — none in hours of idle polling.

Use `scripts/analyze_pushes.py` on `.raw.jsonl` captures to diff payloads while
reverse-engineering further settings. Capture one change at a time with a
wall-clock note; see `logs/re/README.md`.

---

## 7. Zone configuration payload (`0xa154`)

The zone-config body written by §6.2 (and broadcast by a satellite) is:

```
[comfort temp] [reduced temp] [frost temp] [mode] [options] [00] [weekly schedule…]
```

- Temperatures are 1-byte (`value = (°C × 10) − 50`, i.e. 0.1 °C steps).
- **mode** byte: `0x05` auto · `0x06` comfort · `0x07` reduced · `0x08` frost.
- **options** byte encodes the *mode class*, and is **derived from the target
  mode**, not carried over from a previous state:

  | options | Meaning |
  |--------:|---------|
  | `0x10` | Auto, following the weekly program (no derogation). |
  | `0x20` | Frost / off. |
  | `0x24` | Fixed eco level (permanent reduced, or an auto eco-derogation). |
  | `0x25` | Fixed comfort level (permanent comfort, or an auto comfort-derogation); `+0x40` adds boost. |

  A **derogation** (temporary override within auto) is expressed by keeping the
  **mode byte at `0x05` (auto)** while the options switch from the schedule form
  (`0x10`) to a fixed form (`0x24`/`0x25`). There is **no separate "derogation"
  bit**. Sending an inconsistent pair (e.g. mode auto with options `0x20`) is
  rejected by the satellite.

- **weekly schedule**: 7 days × 6 bytes = 48 half-hour slots per day, each slot a
  bit selecting comfort vs reduced. Present on a full write; omitted on a short
  write (setpoints/mode only). Time-of-day resolution of this program is done by
  the satellite, not the boiler.

---

## 8. Virtual satellite (the reliable control path)

Because a physical satellite's sleep cycle makes pushed commands unreliable
(§5), the robust way to control a zone is to **become** its satellite:

- The bridge is always awake, so it owns the zone's mode, setpoints and program,
  resolves the **active setpoint**, and reports it to the boiler via a consigne
  (`0xa02f`, §6.3) — which the boiler accepts and ACKs directly and immediately.
- Room temperature must be supplied from an external source (e.g. a Home
  Assistant sensor), since the bridge has no thermometer of its own.

Implications:

- **One controller per zone.** The physical satellite for that zone must be
  removed/unpaired, or both will fight on address `0x08`.
- **The bridge becomes safety-critical** for the zone: if it stops, the zone
  loses its thermostat. A fallback (keep reporting the last setpoint) is prudent.
- In `auto`, the consigne reports the comfort or eco level according to the
  comfort/derogation flag. Flipping comfort↔eco automatically on the weekly
  clock requires the controller to resolve the 48-slot/day program against the
  current time — a capability the satellite owns (the boiler does not do it).

---

## 9. Quick reference

- One half-duplex channel; one network id; fixed per-device addresses.
- `0x80` boiler is the hub; it relays but does **not** buffer Connect zone writes.
- Satellites sleep; relayed writes only land in a wake window; a landed write is
  ACKed with `control=0xFE`.
- Reads and satellite consignes (`control=0x01`/`0x81`) are direct and reliable.
- A zone's options byte must match its mode; derogation = auto mode byte + fixed
  options.
- Virtual satellite = always-awake controller using the consigne path = reliable.
