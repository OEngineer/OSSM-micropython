"""Stroke patterns and PatternInput — ported from ossm/ pattern-engine."""

import asyncio


class PatternInput:
    """Live-updateable motion parameters, shared between engine and tasks."""

    def __init__(self):
        self.depth = 0.0      # 0.0–1.0: how deep (fraction of machine range)
        self.stroke = 0.0     # 0.0–1.0: stroke length as fraction of depth
        self.velocity = 0.0   # 0.0–1.0: fraction of max speed
        self.sensation = 0.0  # -1.0 to 1.0: pattern-specific modifier
        self.buffer = 0.0     # 0.0–1.0: streaming buffer
        
    def __repr__(self):
        return f"PatternInput(depth={self.depth}, stroke={self.stroke}, velocity={self.velocity}, sensation={self.sensation}, buffer={self.buffer})"


# Pattern metadata: (name, description)
# Order matches upstream StrokePatterns enum:
#   SimpleStroke=0, TeasingPounding=1, RoboStroke=2, HalfnHalf=3,
#   Deeper=4, StopNGo=5, Insist=6
PATTERNS = [
    ("Simple Stroke",
     "Simple in and out. Sensation does nothing."),
    ("Teasing Pounding",
     "Alternating strokes. Sensation controls in/out speed ratio."),
    ("Robo Stroke",
     "Robotic strokes. Sensation adjusts speed character."),
    ("Half'n'Half",
     "Alternate full and half strokes. Sensation controls speed ratio."),
    ("Deeper",
     "Goes deeper with every stroke. Sensation controls step count."),
    ("Stop'n'Go",
     "Stops after a series of strokes. Sensation controls the delay."),
    ("Insist",
     "Short rapid strokes. Sensation shifts position and stroke length."),
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

    When depth is not set (0.0), falls back to stroke as the deep end so
    that apps sending only set:stroke still produce motion.
    """
    depth = max(inp.depth, inp.stroke)
    shallow = max(0.0, depth - inp.stroke)
    return shallow + frac * (depth - shallow)


async def _move(ctrl, inp, position_frac, speed_factor=1.0):
    """Move to position_frac within the stroke range and wait.

    Polls inp.velocity every 20 ms so speed changes take effect mid-move.
    When velocity drops to zero, stops the motor and waits until it resumes,
    then re-issues the move to the original target.
    Propagates CancelledError to support task cancellation.
    """
    pos = _pattern_pos(inp, position_frac)

    # Don't start a move while paused — wait for a non-zero velocity first.
    while inp.velocity == 0.0:
        await asyncio.sleep_ms(20)

    spd = inp.velocity * max(0.0, min(1.0, speed_factor))
    ctrl.move_to(pos, spd)

    last_velocity = inp.velocity
    try:
        while ctrl.moving:
            await asyncio.sleep_ms(20)
            if inp.velocity != last_velocity:
                if inp.velocity == 0.0:
                    ctrl.stop()
                    # Wait for deceleration to complete, then for resume.
                    while ctrl.moving:
                        await asyncio.sleep_ms(20)
                    while inp.velocity == 0.0:
                        await asyncio.sleep_ms(20)
                    # Re-issue the move to the original target at the new speed.
                    ctrl.move_to(pos, inp.velocity * speed_factor)
                else:
                    ctrl.update_speed(inp.velocity * speed_factor)
                last_velocity = inp.velocity
        await asyncio.sleep_ms(0)  # always yield; prevents starvation on zero-distance moves
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


async def teasing_pounding(ctrl, inp):
    """Alternating strokes with asymmetric speed. Sensation controls ratio."""
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


async def robo_stroke(ctrl, inp):
    """Robotic strokes. Sensation scales speed character.

    Approximates the upstream RoboStroke trapezoid-profile variation:
    higher sensation → higher speed → more constant-speed feel;
    lower sensation → lower speed → more triangular feel.
    """
    while True:
        factor = max(0.1, 1.0 + inp.sensation * 0.5)  # 0.5–1.5
        await _move(ctrl, inp, 1.0, factor)
        await _move(ctrl, inp, 0.0, factor)


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


async def deeper(ctrl, inp):
    """Progressively deeper strokes. Sensation controls step count (2-32)."""
    MIN_STEPS = 2
    MAX_STEPS = 32
    while True:
        num_steps = max(
            1, round(_scale(inp.sensation, -1.0, 1.0, MIN_STEPS, MAX_STEPS))
        )
        for step in range(1, num_steps + 1):
            await _move(ctrl, inp, step / num_steps)
            await _move(ctrl, inp, 0.0)


async def stop_n_go(ctrl, inp):
    """Stroke N times then pause. Sensation controls pause duration."""
    MAX_STROKES = 5
    MIN_DELAY_MS = 100
    MAX_DELAY_MS = 10_000
    num_strokes = 1
    counting_up = True
    while True:
        for _ in range(num_strokes):
            await _move(ctrl, inp, 1.0)
            await _move(ctrl, inp, 0.0)
        delay_ms = round(
            _scale(inp.sensation, -1.0, 1.0, MIN_DELAY_MS, MAX_DELAY_MS)
        )
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


async def insist(ctrl, inp):
    """Short rapid strokes. Sensation shifts position and stroke length.

    Positive sensation → strokes wander toward shallow end.
    Negative sensation → strokes wander toward deep end.
    Higher |sensation| → shorter effective stroke (more vibrational).
    """
    while True:
        sensation = inp.sensation
        abs_s = abs(sensation)
        # Stroke shrinks to ~5% at max |sensation|
        stroke_frac = max(0.05, 1.0 - abs_s * 0.9)
        # Center shifts: +1→0.1 (shallow), -1→0.9 (deep)
        center = 0.5 - sensation * 0.4
        lo = max(0.0, center - stroke_frac * 0.5)
        hi = min(1.0, center + stroke_frac * 0.5)
        await _move(ctrl, inp, hi)
        await _move(ctrl, inp, lo)


PATTERN_FUNCS = [
    simple_stroke, teasing_pounding, robo_stroke, half_n_half,
    deeper, stop_n_go, insist,
]
