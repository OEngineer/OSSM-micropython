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
        self.latency_comp = False
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
        """Consume stream targets and execute them.

        When self.latency_comp is True, applies the upstream buffer-based
        timing correction: adjusts each move duration to absorb jitter,
        shortening by up to 1/4 or extending by up to buffer*2 ms.
        """
        range_mm = config.MAX_MM - config.MIN_MM
        # Latency compensation state
        best_ms = time.ticks_ms()   # ideal-timeline clock
        last_time_ms = 0            # nominal duration of the previous move
        try:
            while True:
                try:
                    pos_frac, time_ms = await asyncio.wait_for_ms(
                        self._stream_queue.get(), 5000
                    )
                except asyncio.TimeoutError:
                    # Long idle — reset the ideal timeline so stale lag
                    # doesn't corrupt the next burst of moves.
                    best_ms = time.ticks_ms()
                    last_time_ms = 0
                    continue
                # Apply depth/stroke windowing matching upstream streaming.cpp.
                # BLE pos_frac: 0.0 = minimum (shallow/retracted), 1.0 = maximum (deep/extended).
                # maxStroke = min(stroke, depth) caps stroke at depth.
                # depth_offset positions the window within the full travel range.
                inp = self.inp
                # Gap A (fixed): apply sensation as acceleration limit (upstream
                # streaming.cpp line 92: accelLimit = maxAccel * sensation/100).
                # inp.sensation is [-1, 1]; map to [0, 1] matching upstream 0-100.
                sensation_frac = max(0.01, (inp.sensation + 1.0) / 2.0)
                self._ctrl.update_accel(sensation_frac)
                max_stroke = min(inp.stroke, inp.depth)
                depth_offset = (1.0 - max_stroke) * inp.depth
                target = pos_frac * max_stroke + depth_offset
                current = self._ctrl.position_frac
                dist_mm = abs(target - current) * range_mm

                # Gap B: latency compensation (upstream streaming.cpp lines 67-85).
                # Measures how far behind the ideal timeline we are and shortens or
                # extends the move time to stay synchronised with the client clock.
                # inp.buffer (0-1) maps to upstream settings.buffer (0-100) in ms.
                adjusted_time_ms = time_ms
                if self.latency_comp and last_time_ms > 0:
                    now_ms = time.ticks_ms()
                    current_buffer_ms = time.ticks_diff(now_ms, best_ms)
                    buffer_ms = inp.buffer * 100.0
                    mincomp = int(min(buffer_ms * 2, last_time_ms))
                    offset = mincomp - current_buffer_ms
                    if offset < 0:
                        offset = max(-(time_ms // 4), offset)
                    adjusted_time_ms = max(1, time_ms + offset)

                # Advance ideal timeline by the nominal (unadjusted) duration.
                best_ms = time.ticks_add(best_ms, time_ms)
                last_time_ms = time_ms

                if adjusted_time_ms > 0 and dist_mm > 0:
                    speed_frac = (
                        (dist_mm * 1000 / adjusted_time_ms) / config.MAX_SPEED_MM_S
                    )
                else:
                    speed_frac = 1.0
                speed = max(0.01, min(1.0, speed_frac))
                if not self._ctrl.retarget(target, speed):
                    # Direction change — must wait for current move to finish.
                    await self._ctrl.wait_done()
                    self._ctrl.move_to(target, speed)
        finally:
            self._ctrl.update_accel(1.0)  # restore max accel on exit

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
