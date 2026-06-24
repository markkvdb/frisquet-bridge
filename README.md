# frisquet-bridge

Open-source bridge between a **Frisquet Eco Radio Visio** boiler and **Home Assistant**. This project makes it possible to control the boiler fully local within Home Assistant. All features of the Connect box and satellites without having to buy any hardware or cloud connection.

Combines the protocol work from [frisquet-connect](https://github.com/d33d33/frisquet-connect) with the richer feature set of [OpenFrisquetVisio](https://github.com/freedomnx/OpenFrisquetVisio), while keeping a clean separation:

| Layer | Where it runs |
|-------|----------------|
| **RF modem** | Adafruit Feather M0 + RFM69HCW (`firmware/`) |
| **Protocol + HA logic** | Python service (`src/frisquet_bridge/`) |

## Hardware

- [Adafruit Feather M0 RFM69HCW](https://www.adafruit.com/product/3178) (868 MHz)
- USB connection to a Raspberry Pi (or any Linux host)

## Quick start: flash firmware + listen

### 1. Install PlatformIO

```bash
pip install platformio
# or: curl -fsSL https://raw.githubusercontent.com/platformio/platformio-core/develop/scripts/get-platformio.py | python3 -
```

### 2. Build and upload firmware

Connect the Feather via USB, then:

```bash
cd firmware
pio run -t upload
```

If the port is not auto-detected:

```bash
pio run -t upload --upload-port /dev/serial/by-id/usb-Adafruit_Feather_M0_9E497F10503052534C2E3120FF150323-if00
```

Replace with your device path from `ls /dev/serial/by-id/`.

Monitor serial output (optional):

```bash
pio device monitor
```

You should see `READY frisquet-bridge-fw 1.0.0` **once at boot** (press the **RESET** button while the monitor is open if you connected late). At any time you can also query:

```
VERSION aa
```

which returns `READY ...`, `INFO version 1.0.0`, and `OK 0`. Heartbeats appear as `HB` every 30 seconds in LISTEN mode.

### 3. Run the RF sniffer (Python tools via uv)

Install [uv](https://docs.astral.sh/uv/) once, then from the repo root:

```bash
uv sync
uv run frisquet-bridge listen
```

Listen on the network ID from `config.toml` (recommended first test — hears your official Frisquet Connect box and the boiler):

```bash
uv run frisquet-bridge listen
```

Or pass the network ID explicitly:

```bash
uv run frisquet-bridge listen --network-id 05d97f78
```

`--promiscuous` uses sync word `ffffffff`, which is useful for pairing/association traffic. It is not a wildcard for every Frisquet network; normal traffic still requires the correct sync word.

After `uv sync`, you can also activate the venv and run directly:

```bash
source .venv/bin/activate
frisquet-bridge listen --help
```

### Run the bridge service

Copy and edit config:

```bash
cp config.toml.example config.toml
```

Pair (first time, boiler in association mode):

```bash
uv run frisquet-bridge pair --config config.toml
```

For outside sensor support, pair the exterior sensor identity too:

```bash
uv run frisquet-bridge pair sonde --config config.toml
```

For modes where the bridge emulates a thermostat, pair the satellite identity
for the zone it will own:

```bash
uv run frisquet-bridge pair satellite_z1 --config config.toml
```

One-shot sensor read:

```bash
uv run frisquet-bridge read --config config.toml
```

Send a virtual outside temperature through the `[frisquet.sonde]` identity:

```bash
uv run frisquet-bridge outside-temp --config config.toml 12.5
```

### Home Assistant setup

The bridge publishes MQTT discovery messages for Home Assistant. Enable the MQTT integration in Home Assistant first, then configure `config.toml` like this:

```toml
[frisquet]
network_id = "05d97f78"
boiler_addr = "80"
sensor_poll_interval_seconds = 30

[frisquet.connect]
mode = "read"
association_id = "ea"

[frisquet.sonde]
enabled = true
association_id = "9c"

[frisquet.zone1]
mode = "satellite"

[frisquet.zone2]
mode = "disabled"

[frisquet.zone3]
mode = "disabled"

[mqtt]
enabled = true
host = "127.0.0.1"
port = 1883
username = ""
password = ""
client_id = "frisquet-bridge"
base_topic = "frisquet"

[logging]
level = "INFO"
format = "console" # use "json" for machine ingestion
file = "logs/frisquet-bridge.log"
file_max_bytes = 10000000
file_backup_count = 5
raw_file = "logs/frisquet-bridge.raw.jsonl"
raw_max_bytes = 50000000
raw_backup_count = 5
raw_lines = false
```

Use your actual `network_id`, association IDs, serial port, and MQTT credentials.
Use `[frisquet.connect] mode = "read"` to publish boiler-related state without
commands, or `mode = "full"` to also allow DHW writes and physical-satellite
commands. With `mode = "passive"`, the bridge observes a real Connect without
transmitting as Connect.
Use `sensor_poll_interval_seconds` to tune active boiler sensor polling; the
default is 30 seconds.
Pick one mode per zone:

| Mode | Use when | Notes |
|------|----------|-------|
| `disabled` | zone is unused | no HA zone entities |
| `satellite` | you keep the physical satellite | read-only unless Connect is `mode = "full"` |
| `virtual_satellite` | the bridge fully owns a zone with the full climate surface | requires satellite identity |
| `simple_satellite` | the bridge owns a zone but HA handles scheduling | only `off`/`heat`, `comfort`/`eco`, setpoint, ambient |
| `central_boiler` | TRVs/VTherm control rooms and the bridge proxies one central boiler | zone 1 only; VTherm drives the demand switch |

Bridge-owned satellite modes (`virtual_satellite`, `simple_satellite`, and
`central_boiler`) need `association_id` in that zone. Rolling request IDs,
mutable temperatures, and central-boiler tuning are stored in
`config.state.json`, not in `config.toml`.

Common zone snippets:

```toml
# Passive physical satellite
[frisquet.zone1]
mode = "satellite"

# Bridge-owned simple thermostat
[frisquet.zone1]
mode = "simple_satellite"
association_id = "c0"

# Single proxy central boiler for TRV/VTherm setups
[frisquet.zone1]
mode = "central_boiler"
association_id = "c0"

[frisquet.zone2]
mode = "disabled"

[frisquet.zone3]
mode = "disabled"
```

With `[frisquet.sonde] enabled = true`, HA also gets an outside-temperature
number entity that writes through the exterior-sensor identity.

For a concise explanation of every config setting and the operating modes, see
[`docs/CONFIG.md`](docs/CONFIG.md).

Run the service:

```bash
uv run frisquet-bridge serve --config config.toml
```

For a diagnostic run with more detail and raw RF frame capture:

```bash
uv run frisquet-bridge serve --config config.toml \
  --log-level DEBUG \
  --log-file logs/frisquet-bridge.log \
  --raw-log-file logs/frisquet-bridge.raw.jsonl
```

The normal log is structured by `structlog` and rotates according to `[logging]`.
The raw log is JSON Lines and stores decoded frame metadata plus exact RF bytes;
add `--raw-lines` only when you also need every host/modem ASCII line.

In Home Assistant, look for the MQTT-discovered device named **Frisquet Bridge**.
Expected entities depend on the mode:

- Boiler sensors when `[frisquet.connect]` is configured; consumption, pressure, and `DHW mode` need `mode = "read"` or `"full"` for active polling
- `Outside temperature` when `[frisquet.sonde]` is enabled
- Zone climate entities for `satellite`, `virtual_satellite`, and `simple_satellite`
- Central demand switch and tuning numbers for `central_boiler`

### Systemd deployment

The sample unit in [deploy/frisquet-bridge.service](deploy/frisquet-bridge.service)
runs the entry point from `.venv` by absolute path. It does not depend on an
activated shell environment, but the virtual environment must be created before
starting the service:

```bash
uv sync --locked --no-dev
cp config.toml.example config.toml
$EDITOR config.toml
sudo cp deploy/frisquet-bridge.service /etc/systemd/system/frisquet-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now frisquet-bridge
```

After pulling a new version, refresh the pinned environment and restart:

```bash
uv sync --locked --no-dev
sudo systemctl restart frisquet-bridge
```

### Troubleshooting upload

| Issue | Fix |
|-------|-----|
| Permission denied on `/dev/ttyACM0` | `sudo usermod -aG dialout $USER` then log out/in |
| Board not found | Double-tap RESET → bootloader port appears briefly; retry upload |
| Wrong port | Use full `/dev/serial/by-id/...` path |
| `radio_init_failed` on boot | Check antenna is connected; board is 868 MHz variant |

## Protocol

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the host ↔ modem serial protocol.

## Project layout

```
frisquet-bridge/
├── pyproject.toml     # Python package (uv)
├── config.toml.example
├── src/frisquet_bridge/   # CLI + protocol + optional MQTT adapter
├── firmware/          # PlatformIO — Feather M0 RFM69 modem
├── deploy/            # systemd unit
└── docs/
    └── PROTOCOL.md
```

## Development

### Run tests

```bash
uv sync --group dev
uv run pytest
```

### Lint and format

```bash
uv run ruff check .
uv run ruff format .
```

## License

MIT (experimental — not affiliated with Frisquet)
