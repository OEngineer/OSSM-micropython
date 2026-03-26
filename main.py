"""OSSM-mpy — MicroPython OSSM firmware for RP2350 (Pico 2 W).

Drive a stepper motor via step/direction signals using stepper-lib.
BLE remote control via the standard OSSM protocol.

Dependencies (copy to /lib/ on the device):
  smartstepper/  — from https://github.com/bikeNomad/stepper-lib
  aioble/        — from micropython-lib bluetooth/aioble

Deploy:
  mpremote cp -r src/ :
  mpremote cp main.py :
"""

import asyncio
import aiorepl

from src.motion import MotionController
from src.pattern_engine import PatternEngine
from src.ble_remote import BleRemote

# make these available to aiorepl as globals
ctrl = MotionController()
engine = PatternEngine(ctrl)
ble = BleRemote(engine)

# accessors for aiorepl
aiorepl_globals = {
    "ctrl": ctrl,
    "engine": engine,
    "ble": ble,
    "asyncio": asyncio,
}
repl = asyncio.create_task(aiorepl.task(aiorepl_globals))

async def main():
    await asyncio.gather(
        engine.run(),
        ble.run(),
        repl
    )


asyncio.run(main())
