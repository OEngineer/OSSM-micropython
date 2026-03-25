"""PatternEngine — manages pattern task lifecycle and shared PatternInput."""

import asyncio
import random
import time

from primitives.queue import Queue
from .patterns import PatternInput, PATTERN_FUNCS
from . import config


class EngineState:
    IDLE = "idle"
    HOMING = "homing"
    READY = "ready"
    PLAYING = "playing"
    STREAMING = "streaming"
    PAUSED = "paused"


class PatternEngine:
    def __init__(self, ctrl):
        self._ctrl = ctrl
        self.inp = PatternInput()
        self.state = EngineState.IDLE
        self.pattern_index = 0
        self._task = None
        self._state_cb = None  # called on state/inp changes (for BLE notify)
        self._stream_queue = Queue()
        self._session_id = f"{random.getrandbits(32):08x}"

    def set_state_callback(self, cb):
        self._state_cb = cb

    def _notify(self):
        if self._state_cb:
            self._state_cb()

    # ------------------------------------------------------------------ #
    # Commands (called from BLE handler)                                   #
    # ------------------------------------------------------------------ #

    def home_and_play(self):
        """Start the home-then-play sequence as a background task."""
        asyncio.create_task(self._home_and_play())

    async def _home_and_play(self):
        await self._cancel_task()
        self.state = EngineState.HOMING
        self._notify()
        try:
            await self._ctrl.home()
        except Exception as e:
            print(f"Homing failed: {e}")
            self.state = EngineState.IDLE
            self._notify()
            return
        self.state = EngineState.READY
        self._notify()
        await self._start_pattern()

    async def play(self, index=None):
        """Play pattern by index (or replay current if index is None)."""
        if index is not None:
            self.pattern_index = max(0, min(len(PATTERN_FUNCS) - 1, index))
        await self._cancel_task()
        self._ctrl.enable()
        await self._start_pattern()

    async def stop(self):
        """Stop current pattern and return to Ready state."""
        await self._cancel_task()
        self._ctrl.stop()
        self.state = EngineState.READY
        self._notify()

    def update_input(self, field, value):
        """Update a PatternInput field live; velocity takes immediate effect."""
        setattr(self.inp, field, value)
        if field == "velocity" and self.state == EngineState.PLAYING:
            self._ctrl.update_speed(value)
        self._notify()

    def start_streaming(self):
        """Enter streaming mode (homes first if not already homed)."""
        asyncio.create_task(self._start_streaming())

    async def _start_streaming(self):
        await self._cancel_task()
        already_homed = self.state in (
            EngineState.READY, EngineState.PLAYING, EngineState.STREAMING
        )
        if not already_homed:
            self.state = EngineState.HOMING
            self._notify()
            try:
                await self._ctrl.home()
            except Exception as e:
                print(f"Homing failed: {e}")
                self.state = EngineState.IDLE
                self._notify()
                return
        self.state = EngineState.STREAMING
        self._notify()
        self._task = asyncio.create_task(self._streaming_pattern())

    def stream_target(self, position_frac, time_ms):
        """Queue a streaming position target (called by BLE handler)."""
        try:
            self._stream_queue.put_nowait((position_frac, time_ms))
        except Exception:
            pass  # drop if queue full

    async def _streaming_pattern(self):
        """Consume stream targets and execute them."""
        range_mm = config.MAX_MM - config.MIN_MM
        while True:
            try:
                pos_frac, time_ms = await asyncio.wait_for_ms(
                    self._stream_queue.get(), 5000
                )
            except asyncio.TimeoutError:
                continue
            current = self._ctrl.position_frac
            dist_mm = abs(pos_frac - current) * range_mm
            if time_ms > 0 and dist_mm > 0:
                speed_frac = (dist_mm * 1000 / time_ms) / config.MAX_SPEED_MM_S
            else:
                speed_frac = 1.0
            self._ctrl.move_to(pos_frac, max(0.01, min(1.0, speed_frac)))
            await self._ctrl.wait_done()

    # ------------------------------------------------------------------ #
    # Internal                                                              #
    # ------------------------------------------------------------------ #

    async def _start_pattern(self):
        self.state = EngineState.PLAYING
        self._notify()
        self._task = asyncio.create_task(
            PATTERN_FUNCS[self.pattern_index](self._ctrl, self.inp)
        )

    async def _cancel_task(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"Pattern task error: {e}")
            self._task = None

    async def run(self):
        """Monitor loop — restart task if it exits unexpectedly."""
        while True:
            await asyncio.sleep_ms(500)
            if (
                self.state in (EngineState.PLAYING, EngineState.STREAMING)
                and self._task is not None
                and self._task.done()
            ):
                print("Pattern task exited unexpectedly, restarting")
                if self.state == EngineState.STREAMING:
                    self._task = asyncio.create_task(self._streaming_pattern())
                else:
                    await self._start_pattern()

    # ------------------------------------------------------------------ #
    # State for BLE notifications                                           #
    # ------------------------------------------------------------------ #

    def state_dict(self):
        """Return current state as a dict for JSON serialisation."""
        return {
            "timestamp": time.ticks_ms(),
            "state": self.state,
            "speed": round(self.inp.velocity * 100),
            "stroke": round(self.inp.stroke * 100),
            "sensation": round((self.inp.sensation + 1.0) / 2.0 * 100),
            "depth": round(self.inp.depth * 100),
            "pattern": self.pattern_index,
            "position": round(self._ctrl.position_frac * 100),
            "sessionId": self._session_id,
        }
