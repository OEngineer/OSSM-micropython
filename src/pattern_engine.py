"""PatternEngine — manages pattern task lifecycle and shared PatternInput."""

import asyncio
import random
import time

from primitives.queue import Queue
from .patterns import PatternInput, PATTERN_FUNCS, PATTERNS
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
        self._prior_state = self.state
        self.pattern_index = 0
        self._task = None
        self._state_cb = None  # called on state/inp changes (for BLE notify)
        self._stream_queue = Queue()
        self._session_id = f"{random.getrandbits(32):08x}"

    def set_state_callback(self, cb):
        self._state_cb = cb

    def _notify(self):
        if self._prior_state != self.state:
            print(f"Engine state: {self.state}")
            self._prior_state = self.state
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
        if field == "velocity" and self.state == EngineState.PLAYING and value > 0.0:
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
        """Consume stream targets and execute them one at a time."""
        range_mm = config.MAX_MM - config.MIN_MM
        try:
            while True:
                # Block until a command arrives (timeout resets on idle)
                try:
                    pos_frac, time_ms = await asyncio.wait_for_ms(
                        self._stream_queue.get(), 5000
                    )
                except asyncio.TimeoutError:
                    continue

                # Drain queue — use only the freshest command
                while True:
                    try:
                        pos_frac, time_ms = self._stream_queue.get_nowait()
                    except Exception:
                        break

                # Depth/stroke windowing (upstream streaming.cpp)
                inp = self.inp
                sensation_frac = max(0.01, (inp.sensation + 1.0) / 2.0)
                max_stroke = min(inp.stroke, inp.depth)
                depth_offset = (1.0 - max_stroke) * inp.depth
                target = pos_frac * max_stroke + depth_offset
                current = self._ctrl.position_frac
                dist_mm = abs(target - current) * range_mm

                time_s = time_ms / 1000.0
                accel_mm = sensation_frac * config.MAX_ACCEL_MM_S2
                speed_lim = inp.velocity * config.MAX_SPEED_MM_S

                if speed_lim < 1.0 or accel_mm < 1.0:
                    continue

                step_mm = 1.0 / config.STEPS_PER_MM
                if time_s <= 0.01 or dist_mm <= step_mm:
                    continue

                # Clamp distance to what's feasible
                max_d = accel_mm * (time_s / 2) ** 2
                max_d = min(max_d, speed_lim * time_s)
                if dist_mm > max_d and max_d > step_mm:
                    ratio = max_d / dist_mm
                    target = current + (target - current) * ratio
                    dist_mm = max_d

                # Trapezoidal motion profile
                req_spd = (2 * dist_mm) / time_s
                req_spd = max(1.0, min(speed_lim, req_spd))
                vt = req_spd * time_s
                prop = max(0.01, -((2 * dist_mm - 2 * vt) / vt))
                req_acc = req_spd / (time_s * prop / 2)
                req_acc = max(1.0, min(accel_mm, req_acc))

                self._ctrl.update_accel(req_acc / config.MAX_ACCEL_MM_S2)
                speed = max(0.01, min(1.0, req_spd / config.MAX_SPEED_MM_S))

                # Wait for current move to complete, then start fresh
                await self._ctrl.wait_done()
                self._ctrl.move_to(target, speed)
        finally:
            self._ctrl.update_accel(1.0)

    # ------------------------------------------------------------------ #
    # Internal                                                              #
    # ------------------------------------------------------------------ #

    async def _start_pattern(self):
        self.state = EngineState.PLAYING
        self._notify()
        self._task = asyncio.create_task(
            PATTERN_FUNCS[self.pattern_index](self._ctrl, self.inp)
        )
        print(f"Pattern {PATTERNS[self.pattern_index][0]} started: {PATTERNS[self.pattern_index][1]}")

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
        await self._ctrl.wait_done()  # ensure motor finishes decelerating before next move

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
