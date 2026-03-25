"""Stroke patterns and PatternInput — ported from ossm/ pattern-engine."""

import asyncio


class PatternInput:
    """Live-updateable motion parameters, shared between engine and pattern tasks."""

    def __init__(self):
        self.depth = 0.0      # 0.0–1.0: how deep (fraction of machine range)
        self.stroke = 0.0     # 0.0–1.0: stroke length as fraction of depth
        self.velocity = 0.0   # 0.0–1.0: fraction of max speed
        self.sensation = 0.0  # -1.0 to 1.0: pattern-specific modifier


# Pattern metadata: (name, description)
PATTERNS = [
    ("Simple Stroke", "Simple in and out. Sensation does nothing."),
    ("Deeper", "Goes deeper with every stroke. Sensation controls the number of steps."),
    ("Half'n'Half", "Alternate full and half strokes. Sensation controls speed ratio."),
    ("Stop'n'Go", "Stops after a series of strokes. Sensation controls the delay."),
    ("Teasing Pounding", "Alternating strokes. Sensation controls in/out speed ratio."),
]


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _scale(value, in_min, in_max, out_min, out_max):
    """Linear map from [in_min, in_max] to [out_min, out_max]."""
    if in_max == in_min:
        return out_min
    t = (value - in_min) / (in_max - in_min)
    return out_min + t * (out_max - out_min)


def _pattern_pos(inp, frac):
    """Map pattern fraction [0,1] to machine position fraction [0,1].

    0.0 → shallow end (depth - stroke), 1.0 → deep end (depth).
    Mirrors compute_command() in the reference firmware.
    """
    shallow = max(0.0, inp.depth - inp.stroke)
    return shallow + frac * (inp.depth - shallow)


async def _move(ctrl, inp, position_frac, speed_factor=1.0):
    """Move to position_frac within the stroke range and wait for completion.

    Polls inp.velocity every 20 ms so speed changes take effect mid-move.
    Propagates CancelledError to support task cancellation.
    """
    pos = _pattern_pos(inp, position_frac)
    spd = inp.velocity * max(0.0, min(1.0, speed_factor))
    ctrl.move_to(pos, spd)

    last_velocity = inp.velocity
    try:
        while ctrl.moving:
            await asyncio.sleep_ms(20)
            if inp.velocity != last_velocity:
                ctrl.update_speed(inp.velocity * speed_factor)
                last_velocity = inp.velocity
    except asyncio.CancelledError:
        ctrl.stop()
        raise


# ------------------------------------------------------------------ #
# Patterns (each loops forever until cancelled)                        #
# ------------------------------------------------------------------ #

async def simple_stroke(ctrl, inp):
    """Simple in and out."""
    while True:
        await _move(ctrl, inp, 1.0)
        await _move(ctrl, inp, 0.0)


async def deeper(ctrl, inp):
    """Progressively deeper strokes. Sensation controls step count (2-22)."""
    MIN_STEPS = 2
    MAX_STEPS = 22
    while True:
        num_steps = max(1, round(_scale(inp.sensation, -1.0, 1.0, MIN_STEPS, MAX_STEPS)))
        for step in range(1, num_steps + 1):
            await _move(ctrl, inp, step / num_steps)
            await _move(ctrl, inp, 0.0)


async def half_n_half(ctrl, inp):
    """Alternate full and half strokes. Sensation controls speed ratio."""
    MAX_SCALING = 5.0
    BASE_SPEED = 1.0 / MAX_SCALING
    half = False
    while True:
        sensation = inp.sensation
        factor = _scale(abs(sensation), 0.0, 1.0, 1.0, MAX_SCALING)
        if sensation > 0.0:
            out_speed, in_speed = BASE_SPEED, BASE_SPEED * factor
        elif sensation < 0.0:
            out_speed, in_speed = BASE_SPEED * factor, BASE_SPEED
        else:
            out_speed, in_speed = BASE_SPEED, BASE_SPEED
        depth_frac = 0.5 if half else 1.0
        half = not half
        await _move(ctrl, inp, depth_frac, out_speed)
        await _move(ctrl, inp, 0.0, in_speed)


async def stop_n_go(ctrl, inp):
    """Stroke N times then pause. Sensation controls pause duration (100ms-10s)."""
    MAX_STROKES = 5
    MIN_DELAY_MS = 100
    MAX_DELAY_MS = 10_000
    num_strokes = 1
    counting_up = True
    while True:
        for _ in range(num_strokes):
            await _move(ctrl, inp, 1.0)
            await _move(ctrl, inp, 0.0)
        delay_ms = round(_scale(inp.sensation, -1.0, 1.0, MIN_DELAY_MS, MAX_DELAY_MS))
        await asyncio.sleep_ms(delay_ms)
        if counting_up:
            if num_strokes >= MAX_STROKES:
                counting_up = False
                num_strokes -= 1
            else:
                num_strokes += 1
        elif num_strokes <= 1:
            counting_up = True
            num_strokes += 1
        else:
            num_strokes -= 1


async def teasing_pounding(ctrl, inp):
    """Alternating strokes with asymmetric in/out speed. Sensation controls ratio."""
    MAX_SCALING = 5.0
    BASE_SPEED = 1.0 / MAX_SCALING
    while True:
        sensation = inp.sensation
        factor = _scale(abs(sensation), 0.0, 1.0, 1.0, MAX_SCALING)
        if sensation > 0.0:
            out_speed, in_speed = BASE_SPEED, BASE_SPEED * factor
        elif sensation < 0.0:
            out_speed, in_speed = BASE_SPEED * factor, BASE_SPEED
        else:
            out_speed, in_speed = BASE_SPEED, BASE_SPEED
        await _move(ctrl, inp, 1.0, out_speed)
        await _move(ctrl, inp, 0.0, in_speed)


PATTERN_FUNCS = [simple_stroke, deeper, half_n_half, stop_n_go, teasing_pounding]
