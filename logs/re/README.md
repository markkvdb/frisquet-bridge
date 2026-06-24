# Reverse-engineering captures (`0x10` / `0x45`)

Controlled captures for boiler-initiated pushes. Run the sniffer **before** touching the boiler panel:

```bash
frisquet-bridge listen --raw-log-file logs/re/<experiment>.raw.jsonl
```

## Procedure

1. **Idle baseline** (~30 min, no panel input) — confirms `0x45` is not periodic.
2. **One change at a time** — note wall-clock time and action in a comment file or aloud.
3. **Revert** each setting before the next experiment.
4. **Repeat** identical actions twice; bytes that still differ are timestamps/counters, not settings.

## Analysis

```bash
uv run python scripts/analyze_pushes.py logs/re/*.raw.jsonl --stamp-delta
```

Live annotations appear in the `listen` console via `PassiveReadTracker`.

## Baseline result (existing logs)

Across `official-app.raw.jsonl` (hours of Connect polling) and short bridge sessions,
**no** `0x45`/`0x10` traffic appeared without a panel change. Confirms both types are
event-driven, not heartbeat traffic.

---

## Experiments run (operator notes + decoded findings)

### `holiday-on.raw.jsonl`

> Operator: activated holiday-on, then deactivated it at the very end (not sure the
> deactivation was captured).

One `0x10` → `0xa0f0` push captured:

```
holiday=on start=2026-06-23T00:00:00+00:00 end=2026-06-24T00:00:00+00:00 flag=0x88
```

Body = `1a | start(4) | mid(4)=0 | end(4)`. `start`/`end` are Frisquet timestamps
(byte order 2,3,0,1). The deactivation was not captured here.

### `manual-mode.raw.jsonl`

> Operator: unset, set and unset "manual mode" (something new) on the boiler.

Two `0x10` → `0xa0f0` pushes, **zero date body**, trailing flag toggling `0x88` → `0x08`.
So manual-mode changes reuse the `0xa0f0` block with no dates; the only varying
signal is the change-toggle flag (the `0x80` bit). The manual-mode state itself is
not visible in this push — it likely lives in a block learned via polling.

### `ecs-stop-night-77-23-4.raw.jsonl`

> Operator: ECS set to Stop (the boiler's ECS options differ from the Connect app's).

Three `0x10` → `0xa0f0` pushes, again **zero body**, flag toggling `08 → 88 → 08`.
No `0xa0fc` (DHW mode) push and no `0x45`. Cycling ECS on this boiler does **not**
emit a dedicated mode block over RF; the ECS mode must be read from a polled block.
NOTE: this boiler's ECS options are not MAX/ECO/ECO+/STOP — see open question below.

### `max-temp.raw.jsonl`

> Operator: changed the maximum temperature (radio signals delayed ~30 s).

A `0x45` **clear → set** pair (`kind 0x02` then `0x01`), tag `0x0a8031`, stamp
`ac553a6a → ad553a6a` (low byte +1), all-zero tail. This is the only push type that
fired, so **`0x45` ≈ max-temperature / boiler-parameter change**. The new value is
not yet localized in the payload.

---

## Open questions / next captures

- **ECS options on this boiler.** They differ from the Connect app's
  MAX/ECO/ECO+/STOP. Need the exact on-panel option list to map them. Until then
  the `DhwMode` decode is model-specific. (Capture an `ecs-*` per option and read
  `0xa0fc` via a `READ` before/after.)
- **Holiday-off frame.** Capture a clean deactivation to confirm zero-body + flag.
- **`0x45` value.** Capture several max-temp values to find where the setpoint lands
  in the otherwise-zero tail/stamp.