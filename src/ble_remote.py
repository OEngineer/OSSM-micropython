"""BLE remote — aioble server implementing the OSSM BLE protocol.

Compatible with the Possum app, M5 remote, and configure-ossm tool.
Uses the same service/characteristic UUIDs as the ossm/ reference firmware.

Command format (written to PRIMARY_COMMAND or SPEED_KNOB):
  set:<field>:<0-100>        — update a PatternInput field
  go:strokeEngine            — home motor then play current pattern
  go:simplePenetration       — same as go:strokeEngine
  go:streaming               — home then enter streaming mode
  go:menu                    — stop
  stream:<pos>:<ms>          — move to pos (0-100) in <ms> milliseconds

State notifications (sent on CURRENT_STATE after any change):
  JSON: {"timestamp": ..., "state": "playing", "pattern": 0, ...}
"""

import asyncio
import json
import bluetooth
import aioble

from . import config
from .patterns import PATTERNS, PATTERN_FUNCS

# Initialised once by _register_services()
_service = None
_primary_char = None
_speed_char = None
_state_char = None
_pattern_list_char = None
_pattern_desc_char = None


def _register_services():
    global _service, _primary_char, _speed_char, _state_char
    global _pattern_list_char, _pattern_desc_char

    _service = aioble.Service(bluetooth.UUID(config.SERVICE_UUID))

    _primary_char = aioble.Characteristic(
        _service, bluetooth.UUID(config.PRIMARY_COMMAND_UUID),
        read=True, write=True,
    )
    _speed_char = aioble.Characteristic(
        _service, bluetooth.UUID(config.SPEED_KNOB_UUID),
        read=True, write=True,
    )
    _state_char = aioble.Characteristic(
        _service, bluetooth.UUID(config.CURRENT_STATE_UUID),
        read=True, notify=True,
    )
    _pattern_list_char = aioble.Characteristic(
        _service, bluetooth.UUID(config.PATTERN_LIST_UUID),
        read=True,
    )
    _pattern_desc_char = aioble.Characteristic(
        _service, bluetooth.UUID(config.PATTERN_DESCRIPTION_UUID),
        read=True, write=True,
    )

    aioble.register_services(_service)

    _pattern_list_char.write(json.dumps([p[0] for p in PATTERNS]).encode())
    _pattern_desc_char.write(PATTERNS[0][1].encode())


class BleRemote:
    def __init__(self, engine):
        self._engine = engine
        _register_services()
        self._connection = None
        engine.set_state_callback(self._on_state_change)

    def _on_state_change(self):
        """Send a state notification to the connected central, if any."""
        if self._connection is None:
            return
        data = json.dumps(self._engine.state_dict()).encode()
        try:
            _state_char.notify(self._connection, data)
            idx = self._engine.pattern_index
            _pattern_desc_char.write(PATTERNS[idx][1].encode())
        except Exception as e:
            print(f"BLE notify error: {e}")

    def _handle_command(self, data):
        """Parse and dispatch a command received on PRIMARY_COMMAND."""
        try:
            cmd = data.decode().strip()
        except Exception:
            return
        print(f"BLE cmd: {cmd}")

        if cmd.startswith("set:"):
            parts = cmd.split(":")
            if len(parts) != 3:
                return
            _, field, raw_val = parts
            try:
                val = int(raw_val)
            except ValueError:
                return
            if field == "speed":
                self._engine.update_input("velocity", val / 100.0)
            elif field == "depth":
                self._engine.update_input("depth", val / 100.0)
            elif field == "stroke":
                self._engine.update_input("stroke", val / 100.0)
            elif field == "sensation":
                # BLE 0–100 → internal -1.0–1.0
                scaled = (val / 100.0) * 2.0 - 1.0
                self._engine.update_input("sensation", scaled)
            elif field == "buffer":
                self._engine.update_input("buffer", val / 100.0)
            elif field == "pattern":
                idx = val % len(PATTERN_FUNCS)
                asyncio.create_task(self._engine.play(idx))

        elif cmd == "go:strokeEngine" or cmd == "go:simplePenetration":
            self._engine.home_and_play()

        elif cmd == "go:streaming":
            self._engine.start_streaming()

        elif cmd == "go:menu":
            asyncio.create_task(self._engine.stop())

        elif cmd.startswith("stream:"):
            parts = cmd.split(":")
            if len(parts) == 3:
                try:
                    pos = int(parts[1])
                    time_ms = int(parts[2])
                    self._engine.stream_target(pos / 100.0, time_ms)
                except ValueError:
                    pass

    async def _watch_primary(self, connection):
        """Task: relay writes on PRIMARY_COMMAND to the command handler."""
        while True:
            try:
                await _primary_char.written(timeout_ms=200)
                self._handle_command(_primary_char.read())
            except asyncio.TimeoutError:
                pass

    async def _watch_speed(self, connection):
        """Task: relay writes on SPEED_KNOB to velocity updates."""
        while True:
            try:
                await _speed_char.written(timeout_ms=200)
                raw = _speed_char.read()
                try:
                    val = int(raw.decode().strip())
                    self._engine.update_input("velocity", val / 100.0)
                except (ValueError, UnicodeError):
                    pass
            except asyncio.TimeoutError:
                pass

    async def run(self):
        """Advertise and dispatch commands indefinitely."""
        print(f"BLE advertising as '{config.DEVICE_NAME}'")
        while True:
            connection = await aioble.advertise(
                interval_us=250_000,
                name=config.DEVICE_NAME,
                services=[bluetooth.UUID(config.SERVICE_UUID)],
            )
            print(f"BLE connected: {connection.device}")
            self._connection = connection

            # Send current state immediately on connect
            data = json.dumps(self._engine.state_dict()).encode()
            _state_char.notify(connection, data)

            # Watch both writeable characteristics concurrently
            t_primary = asyncio.create_task(self._watch_primary(connection))
            t_speed = asyncio.create_task(self._watch_speed(connection))

            await connection.disconnected()

            t_primary.cancel()
            t_speed.cancel()
            for t in (t_primary, t_speed):
                try:
                    await t
                except asyncio.CancelledError:
                    pass

            await self._engine.stop()
            self._connection = None
            print("BLE disconnected")
