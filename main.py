"""OSSM-mpy — MicroPython OSSM firmware for RP2350 (Pico 2 W).

Drive a stepper motor via step/direction signals using stepper-lib.
BLE remote control via the standard OSSM protocol.

Dependencies (copy to /lib/ on the device):
  smartstepper/  — from https://github.com/bikeNomad/stepper-lib
  aioble/        — from micropython-lib bluetooth/aioble

Deploy:
  mpremote cp -r src/ :/src/
  mpremote cp main.py :/main.py
"""

import asyncio

from src.motion import MotionController
from src.pattern_engine import PatternEngine
from src.ble_remote import BleRemote


async def main():
    ctrl = MotionController()
    engine = PatternEngine(ctrl)
    ble = BleRemote(engine)

    await asyncio.gather(
        engine.run(),
        ble.run(),
    )


asyncio.run(main())
