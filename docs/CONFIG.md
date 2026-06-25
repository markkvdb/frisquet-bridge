# Configuration Guide

This file explains the ways to run `frisquet-bridge` and what each `config.toml`
setting means.

## Operating Modes

Think in roles. The bridge can act as a Connect gateway, an outside sensor, and
optionally a thermostat/satellite for one or more zones.

| Use case | Configure | What happens |
|---|---|---|
| Observe a real Connect | `[frisquet.connect]` with `mode = "passive"` | The bridge sends no Connect traffic and mirrors what it hears. |
| Read boiler state only | `[frisquet.connect]` with `mode = "read"`, zones `satellite` or `disabled` | The bridge polls boiler sensors, DHW, consumption and clock. It does not own heating zones. |
| Observe a physical satellite | `mode = "satellite"` | The bridge listens to the real satellite and publishes state. It does not send zone commands. |
| Control a real satellite via Connect relay | `[frisquet.connect]` with `mode = "full"` and zone `mode = "satellite"` | The bridge uses the normal Frisquet Connect relay workflow. |
| Replace a physical satellite | `mode = "virtual_satellite"` | The bridge owns the zone and periodically sends ambient/setpoint/mode to the boiler. Remove or unpair the physical satellite for that zone. |
| Simple HA-owned scheduling | `mode = "simple_satellite"` | Like virtual satellite, but exposes only `off`/`heat`, `comfort`/`eco`, target temperature, and HA-reported ambient. |
| TRV/VTherm central boiler | `mode = "central_boiler"` on zone 1 | The bridge owns zone 1 and exposes a demand switch plus tuning numbers for a central-boiler setup. Zones 2 and 3 must be disabled. |

Zone modes describe ownership only. Whether a physical `satellite` zone is
read-only or writable is controlled by `[frisquet.connect].mode`.

## `[frisquet]`

| Setting | Meaning |
|---|---|
| `network_id` | Four-byte RF network id/sync word, as hex. All devices on the boiler network share it. |
| `boiler_addr` | Boiler radio address, usually `"80"`; some boilers use `"84"`. |
| `sensor_poll_interval_seconds` | How often active Connect polling reads fast boiler state, default `30`. |

## `[frisquet.connect]`

| Setting | Meaning |
|---|---|
| `mode` | `passive`, `read`, or `full`. |
| `association_id` | Paired Connect association id, hex byte. |

Connect modes:

- `passive`: no Connect transmissions. Use this with a real official Connect
  when you only want the bridge to observe.
- `read`: active boiler polling; DHW and physical-satellite zones are read-only.
- `full`: active polling, DHW writes, and normal Connect-relayed commands to
  physical `satellite` zones.

`association_id` is required for `read` and `full`. It is optional for
`passive`.

## `[frisquet.sonde]`

| Setting | Meaning |
|---|---|
| `association_id` | Paired outside-sensor association id, hex byte. |
| `enabled` | Enables the virtual outside sensor writer. |

When enabled, Home Assistant gets an outside-temperature number entity. Writes
are sent to the boiler through the sonde identity.

## `[frisquet.zoneN]`

`N` is `1`, `2`, or `3`.

| Setting | Meaning |
|---|---|
| `mode` | One of `disabled`, `satellite`, `virtual_satellite`, `simple_satellite`, `central_boiler`. |
| `association_id` | Required only when the bridge emulates the satellite: `virtual_satellite`, `simple_satellite`, or `central_boiler`. |

Zone mode details:

- `disabled`: no MQTT zone entity.
- `satellite`: read-only physical satellite. The bridge listens and publishes
  state; it becomes writable only when Connect is configured as `mode = "full"`.
- `virtual_satellite`: full climate control; the bridge replaces the satellite.
- `simple_satellite`: bridge replaces the satellite, HA owns schedule logic.
- `central_boiler`: zone 1 only; bridge replaces the satellite and exposes a
  central heat-demand switch for TRV-based systems.

## `[serial]`

| Setting | Meaning |
|---|---|
| `port` | USB serial path for the RF modem. |
| `speed` | Serial baud rate, normally `115200`. |

## `[mqtt]`

| Setting | Meaning |
|---|---|
| `enabled` | Enables Home Assistant MQTT discovery and state publishing. |
| `host`, `port` | MQTT broker address. |
| `username`, `password` | Broker credentials, if needed. |
| `client_id` | MQTT client id for this bridge instance. |
| `base_topic` | Root topic, usually `frisquet`. |
| `language` | Discovery/state display language: `en` or `fr`. Defaults to `en`. |

## `[logging]`

| Setting | Meaning |
|---|---|
| `level` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `format` | `console` or `json`. |
| `file` | Structured application log file. Empty disables file logging. |
| `file_max_bytes`, `file_backup_count` | Rotation settings for `file`. |
| `raw_file` | Raw RF JSONL capture file. Empty disables raw capture. |
| `raw_max_bytes`, `raw_backup_count` | Rotation settings for `raw_file`. |
| `raw_lines` | Also print raw RF lines to the console. |

## State Files

Mutable runtime state is stored beside the config as `config.state.json`:
request ids, learned satellite schedules, learned setpoints, simple-mode
temperatures, and central-boiler tuning. Do not put those values in
`config.toml`; the bridge updates the state file automatically.
