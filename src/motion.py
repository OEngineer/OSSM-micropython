"""MotionController — wraps stepper-lib Axis with OSSM state machine."""

import asyncio
import math
from machine import Pin
from smartstepper import SmartStepper, Axis
from smartstepper import homing as homing_mod

from . import config


class MotionState:
    DISABLED = "disabled"
    ENABLED = "enabled"
    HOMING = "homing"
    READY = "ready"
    PLAYING = "playing"
    PAUSED = "paused"


class MotionController:
    def __init__(self):
        self._stepper = SmartStepper(
            config.STEP_PIN,
            config.DIR_PIN,
            config.ENABLE_PIN,
            accelCurve="smooth2",
        )
        self._stepper.stepsPerUnit = config.STEPS_PER_MM
        self._stepper.maxSpeed = config.MAX_SPEED_MM_S * 0.5  # default 50%
        self._stepper.acceleration = config.MAX_ACCEL_MM_S2
        self._stepper.minSpeed = config.MIN_SPEED_MM_S

        self._axis = Axis(
            self._stepper,
            hard_max_speed=config.MAX_SPEED_MM_S,
            hard_max_accel=config.MAX_ACCEL_MM_S2,
        )

        self._homing_pin = Pin(config.HOMING_PIN, Pin.IN, Pin.PULL_UP)
        self.state = MotionState.DISABLED
        self._last_target = None

    # ------------------------------------------------------------------ #
    # State transitions                                                     #
    # ------------------------------------------------------------------ #

    def enable(self):
        print("Enabling")
        self._axis.enable()
        if self.state == MotionState.DISABLED:
            self.state = MotionState.ENABLED

    def disable(self):
        print("Disabling")
        self._axis.stop(emergency=True)
        self._axis.disable()
        self.state = MotionState.DISABLED

    async def home(self):
        print("Homing")
        self.state = MotionState.HOMING
        self._axis.enable()
        await homing_mod.home(
            self._stepper,
            self._homing_pin,
            fastSpeed=config.FAST_HOMING_SPEED_MM_S,
            slowSpeed=config.SLOW_HOMING_SPEED_MM_S,
            direction=config.HOMING_DIRECTION,
            activeState=config.HOMING_ACTIVE_STATE,
            timeout=(config.MAX_MM / config.FAST_HOMING_SPEED_MM_S) + 5,
        )
        # Map sensor edge to its configured position in the motion coordinate space
        self._stepper.position = config.HOME_SENSOR_MM
        # Move to min_mm so we start in a safe position
        self._stepper.maxSpeed = config.MAX_SPEED_MM_S * 0.3
        self._axis.moveTo(config.MIN_MM)
        await self._axis.wait_done()
        self.state = MotionState.READY
        print("Homed")

    # ------------------------------------------------------------------ #
    # Motion                                                                #
    # ------------------------------------------------------------------ #

    def move_to(self, position_frac, speed_frac):
        """Non-blocking move to position_frac [0,1] at speed_frac [0,1].

        position_frac is within the full machine range (min_mm..max_mm).
        speed_frac is a fraction of max_speed_mm_s.
        Must not be called while moving — use retarget() for streaming.
        """
        mm = self._frac_to_mm(position_frac)
        mm = max(config.MIN_MM, min(config.MAX_MM, mm))
        self._stepper.maxSpeed = self._frac_to_speed(speed_frac)
        if self._last_target != mm:
            print(f"Moving to {mm}mm at {self._stepper.maxSpeed}mm/s")  # remove DEBUG
            self._last_target = mm
        self._axis.moveTo(mm)

    def retarget(self, position_frac, speed_frac):
        """Streaming-optimized move: retargets mid-move for same direction.

        Returns True if the move was started or retargeted.
        Returns False if a direction change is needed (caller must wait_done first).
        """
        mm = self._frac_to_mm(position_frac)
        mm = max(config.MIN_MM, min(config.MAX_MM, mm))
        new_speed = self._frac_to_speed(speed_frac)

        if not self._stepper.moving:
            # Stepper is idle — start a fresh move
            self._stepper.maxSpeed = new_speed
            if self._last_target != mm:
                print(f"Moving to {mm}mm at {self._stepper.maxSpeed}mm/s")
                self._last_target = mm
            self._axis.moveTo(mm)
            return True

        # Stepper is moving — check direction
        current_mm = self._stepper.position
        going_positive = self._stepper._target > current_mm
        want_positive = mm > current_mm

        # If negligible distance, treat as same direction
        if abs(mm - current_mm) < 1.0 / config.STEPS_PER_MM:
            return True

        if going_positive != want_positive:
            return False  # direction change needed

        # Same direction: retarget by updating _target and replanning
        self._stepper._target = mm
        new_speed = self._clamp_speed_for_remaining(new_speed)
        self._stepper.maxSpeed = new_speed  # triggers _replan()
        if self._last_target != mm:
            print(f"Moving to {mm}mm at {self._stepper.maxSpeed}mm/s")
            self._last_target = mm
        return True

    def _clamp_speed_for_remaining(self, speed_mm_s):
        """Limit speed so the motor can decelerate within remaining distance.

        The stepper-lib replanner does this too, but the DMA/PIO pipeline
        buffers 1-2 segments at the old speed.  A safety margin prevents
        those buffered steps from pushing past the target or travel limits.
        """
        if not self._stepper.moving:
            return speed_mm_s
        pos = self._stepper.position
        target = self._stepper._target
        remaining = abs(target - pos)
        # Distance to the nearer travel limit in the direction of motion
        if target > pos:
            limit_margin = config.MAX_MM - pos
        else:
            limit_margin = pos - config.MIN_MM
        # Use the smaller of target-distance and limit-distance
        effective = min(remaining, limit_margin)
        # Reserve a few mm for DMA pipeline lag at high speed
        SAFETY_MM = 2.0
        safe_dist = max(0.0, effective - SAFETY_MM)
        if safe_dist <= 0.0:
            return config.MIN_SPEED_MM_S
        # v_max = sqrt(2·a·d + v_min²)
        max_safe = math.sqrt(
            2.0 * self._stepper.acceleration * safe_dist
            + config.MIN_SPEED_MM_S ** 2
        )
        return max(config.MIN_SPEED_MM_S, min(speed_mm_s, max_safe))

    def update_speed(self, speed_frac):
        """Update speed mid-move, clamped for safe deceleration."""
        new_speed = self._frac_to_speed(speed_frac)
        self._stepper.maxSpeed = self._clamp_speed_for_remaining(new_speed)

    def update_accel(self, accel_frac):
        """Set acceleration as a fraction of max — used by streaming for sensation."""
        self._stepper.acceleration = max(1.0, accel_frac * config.MAX_ACCEL_MM_S2)

    async def wait_done(self):
        await self._axis.wait_done()

    def stop(self, emergency=False):
        if self._stepper.moving:
            self._axis.stop(emergency=emergency)

    # ------------------------------------------------------------------ #
    # Properties                                                            #
    # ------------------------------------------------------------------ #

    @property
    def position_frac(self):
        mm = self._stepper.position
        return (mm - config.MIN_MM) / (config.MAX_MM - config.MIN_MM)

    @property
    def moving(self):
        return self._stepper.moving

    # ------------------------------------------------------------------ #
    # Helpers                                                               #
    # ------------------------------------------------------------------ #

    def _frac_to_mm(self, frac):
        return config.MIN_MM + frac * (config.MAX_MM - config.MIN_MM)

    def _frac_to_speed(self, frac):
        speed = frac * config.MAX_SPEED_MM_S
        return max(config.MIN_SPEED_MM_S, min(config.MAX_SPEED_MM_S, speed))
