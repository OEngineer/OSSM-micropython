"""PatternEngine — manages pattern task lifecycle and shared PatternInput."""

import asyncio

from .motion import MotionState
from .patterns import PatternInput, PATTERNS, PATTERN_FUNCS


class EngineState:
    IDLE = "idle"
    HOMING = "homing"
    READY = "ready"
    PLAYING = "playing"
    PAUSED = "paused"


class PatternEngine:
    def __init__(self, ctrl):
        self._ctrl = ctrl
        self.inp = PatternInput()
        self.state = EngineState.IDLE
        self.pattern_index = 0
        self._task = None
        self._state_cb = None  # called whenever state or inp changes (e.g. for BLE notify)

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
        """Update a PatternInput field live. Speed changes take effect immediately."""
        setattr(self.inp, field, value)
        if field == "velocity" and self.state == EngineState.PLAYING:
            self._ctrl.update_speed(value)
        self._notify()

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
        """Monitor loop — restarts pattern if it exits unexpectedly."""
        while True:
            await asyncio.sleep_ms(500)
            if (
                self.state == EngineState.PLAYING
                and self._task is not None
                and self._task.done()
            ):
                print("Pattern exited unexpectedly, restarting")
                await self._start_pattern()

    # ------------------------------------------------------------------ #
    # State for BLE notifications                                           #
    # ------------------------------------------------------------------ #

    def state_dict(self):
        """Return current state as a dict for JSON serialisation."""
        return {
            "state": self.state,
            "pattern": self.pattern_index,
            "patternName": PATTERNS[self.pattern_index][0],
            "speed": round(self.inp.velocity * 100),
            "depth": round(self.inp.depth * 100),
            "stroke": round(self.inp.stroke * 100),
            "sensation": round((self.inp.sensation + 1.0) / 2.0 * 100),
        }
