# OSSM-micropython

MicroPython firmware for the OSSM (Open Source Sex Machine) on RP2350 (Raspberry Pi Pico 2 W).
Drives a stepper motor via step/direction pulse signals using [stepper-lib](https://github.com/bikeNomad/stepper-lib).
BLE remote control uses the standard OSSM protocol, compatible with the RADR remote (https://github.com/researchanddesire/radr-wireless-remote.git)

## Hardware

- **Board**: Raspberry Pi Pico 2 W (RP2350 + CYW43 BLE/WiFi)
- **Motor interface**: Step + Direction + Enable signals to stepper driver.
- **Homing**: NC (normally-closed) or NO (normally-open) limit switch connected to GND and GPIO

## Differences from the stock OSSM firmware

- **No Display**: all BLE
- **No Wired Remote**: all BLE
- **No Wifi OTA Update**: updating is via `mpremote` currently.
- **Only Step/Direction Interface**: CAN/RS485 currently not implemented

### Default pin assignments

| Signal | GPIO | Notes |
|--------|------|-------|
| STEP   | 2    | Pulse output |
| DIR    | 3    | Direction output |
| ENABLE | 4    | Active-low enable |
| HOMING | 5    | NC limit switch, pulled up |

Edit `src/config.py` to match your wiring.

### Mechanical defaults

| Parameter | Default | Notes |
|-----------|---------|-------|
| Pulley teeth | 20 | |
| Belt pitch | 2.0 mm | |
| Steps/rev | 200 | |
| Microsteps | 8 | Match your driver DIP switches |
| Stroke range | 10–160 mm | Adjust `MAX_MM` to your build |

**Steps per mm** = `steps_per_rev × microsteps / (teeth × pitch)` = 40 steps/mm at the defaults.

## Software architecture

```
main.py                   — asyncio entry point
src/
  config.py               — all hardware and motion constants
  motion.py               — MotionController: state machine + stepper-lib wrapper
  patterns.py             — PatternInput dataclass + 7 stroke patterns
  pattern_engine.py       — PatternEngine: task lifecycle, live parameter updates, streaming
  ble_remote.py           — aioble BLE server, OSSM protocol
```

Two long-running asyncio tasks run concurrently:
1. **`engine.run()`** — monitors the active pattern task, restarts on unexpected exit
2. **`ble.run()`** — advertises, accepts connections, dispatches commands

## Dependencies

Install to `/lib/` on the device:

| Library        | Source                                                                               |
|----------------|--------------------------------------------------------------------------------------|
| `smartstepper` | `mpremote mip install github:bikeNomad/micropython-rp2-smartStepper`                 |
| `aioble`       | `mpremote mip install aioble`                                                        |
| `primitives`   | `mpremote mip install github:peterhinch/micropython-async/v3/primitives`             |


## Deploying

```bash
# Install dependencies
mpremote mip install github:bikeNomad/micropython-rp2-smartStepper
mpremote mip install aioble
mpremote mip install github:peterhinch/micropython-async/v3/primitives

# Copy firmware
mpremote cp -r src/ :/src/
mpremote cp main.py :/main.py
```

## BLE protocol

Compatible with the https://github.com/KinkyMakers/OSSM-hardware.git `main` branch reference firmware.

### Commands

| Command | Effect |
|---------|--------|
| `go:strokeEngine` | Home motor, then start current pattern |
| `go:simplePenetration` | Same as `go:strokeEngine` |
| `go:streaming` | Home (if needed), then enter streaming mode |
| `go:menu` | Stop |
| `set:speed:<0–100>` | Set velocity (fraction of max) |
| `set:depth:<0–100>` | Set depth (fraction of stroke range) |
| `set:stroke:<0–100>` | Set stroke length (fraction of depth) |
| `set:sensation:<0–100>` | Set pattern modifier (mapped to −1.0–1.0) |
| `set:buffer:<0–100>` | Set streaming buffer parameter |
| `set:pattern:<0–6>` | Switch pattern by index |
| `stream:<pos>:<ms>` | Move to position `pos` (0–100) in `ms` milliseconds (streaming mode) |

### State notifications

JSON sent on CURRENT_STATE characteristic after every parameter change or state transition:

```json
{
  "timestamp": 12345,
  "state": "playing",
  "pattern": 0,
  "speed": 50,
  "depth": 60,
  "stroke": 50,
  "sensation": 50,
  "position": 30,
  "sessionId": "a1b2c3d4"
}
```

`state` is one of: `"idle"`, `"homing"`, `"ready"`, `"playing"`, `"streaming"`, `"paused"`.

### BLE UUIDs

Same as `ossm/` reference firmware:

| Characteristic | UUID |
|---------------|------|
| Service | `522b443a-4f53-534d-0001-420badbabe69` |
| PRIMARY_COMMAND | `522b443a-4f53-534d-1000-420badbabe69` |
| SPEED_KNOB | `522b443a-4f53-534d-1010-420badbabe69` |
| CURRENT_STATE | `522b443a-4f53-534d-2000-420badbabe69` |
| PATTERN_LIST | `522b443a-4f53-534d-3000-420badbabe69` |
| PATTERN_DESCRIPTION | `522b443a-4f53-534d-3010-420badbabe69` |

## Patterns

Matches upstream `StrokePatterns` enum order:

| # | Name | Sensation |
|---|------|-----------|
| 0 | Simple Stroke | — |
| 1 | Teasing Pounding | Controls in/out speed ratio |
| 2 | Robo Stroke | Adjusts speed character |
| 3 | Half'n'Half | Controls in/out speed ratio |
| 4 | Deeper | Controls step count (2–32 incremental strokes) |
| 5 | Stop'n'Go | Controls pause duration (100 ms – 10 s) |
| 6 | Insist | Shifts position and stroke length |

All patterns respond to live changes to depth, stroke, and velocity without stopping.

## Streaming mode

`go:streaming` homes the motor (if not already homed) and enters streaming mode. The engine then accepts `stream:<pos>:<ms>` commands, moving to each target position in the requested time. Position applies depth/stroke windowing matching the upstream `streaming.cpp` logic: `pos` 0 = deep end, 100 = shallow end.

## Homing

On `go:strokeEngine` or `go:streaming`, the motor runs a three-phase homing sequence:
1. **Backoff** (if sensor already triggered): jog away at slow speed
2. **Fast approach**: jog toward sensor at 50 mm/s until limit switch triggers
3. **Slow backoff**: jog away at 5 mm/s; stop immediately when switch releases

Position is set to `HOME_SENSOR_MM` (default 0) at the switch release point, then the carriage moves to `MIN_MM` before patterns begin.

## Motion profile

Uses stepper-lib's `smooth2` acceleration curve (smootherstep: zero velocity *and* jerk at endpoints). This approximates the S-curve output of the Ruckig trajectory planner used in the reference firmware.

Speed changes from BLE take effect within 20 ms via stepper-lib's automatic mid-move replanning.

## Reference

- [ossm/ Rust firmware](../ossm/) — primary reference (Embassy + Ruckig + RS485 Modbus RTU)
- [stepper-lib](https://github.com/bikeNomad/stepper-lib) — PIO/DMA pulse generation for RP2040/RP2350
- [OSSM guides](https://docs.researchanddesire.com/ossm/)
