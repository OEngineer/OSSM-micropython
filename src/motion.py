"""MotionController — wraps stepper-lib Axis with OSSM state machine."""

import asyncio
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

    # ------------------------------------------------------------------ #
    # State transitions                                                     #
    # ------------------------------------------------------------------ #

    def enable(self):
        self._axis.enable()
        if self.state == MotionState.DISABLED:
            self.state = MotionState.ENABLED

    def disable(self):
        self._axis.stop(emergency=True)
        self._axis.disable()
        self.state = MotionState.DISABLED

    async def home(self):
        self.state = MotionState.HOMING
        self._axis.enable()
        await homing_mod.home(
            self._stepper,
            self._homing_pin,
            fastSpeed=50.0,
            slowSpeed=5.0,
            direction=config.HOMING_DIRECTION,
            activeState=config.HOMING_ACTIVE_STATE,
            timeout=30,
        )
        # Map sensor edge to its configured position in the motion coordinate space
        self._stepper.position = config.HOME_SENSOR_MM
        # Move to min_mm so we start in a safe position
        self._stepper.maxSpeed = config.MAX_SPEED_MM_S * 0.3
        self._axis.moveTo(config.MIN_MM)
        await self._axis.wait_done()
        self.state = MotionState.READY

    # ------------------------------------------------------------------ #
    # Motion                                                                #
    # ------------------------------------------------------------------ #

    def move_to(self, position_frac, speed_frac):
        """Non-blocking move to position_frac [0,1] at speed_frac [0,1].

        position_frac is within the full machine range (min_mm..max_mm).
        speed_frac is a fraction of max_speed_mm_s.
        """
        mm = self._frac_to_mm(position_frac)
        mm = max(config.MIN_MM, min(config.MAX_MM, mm))
        self._stepper.maxSpeed = self._frac_to_speed(speed_frac)
        self._axis.moveTo(mm)

    def update_speed(self, speed_frac):
        """Update speed mid-move — stepper-lib replans automatically."""
        self._stepper.maxSpeed = self._frac_to_speed(speed_frac)

    async def wait_done(self):
        await self._axis.wait_done()

    def stop(self, emergency=False):
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
