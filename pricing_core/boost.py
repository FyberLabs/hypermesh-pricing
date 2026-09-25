"""Conditional floor boost.

pricing-floor-and-bidding-2026-09-25.md simulated defaults, as implemented
in market-sim: ``b_max`` at or below ``u_low``, linear fade to 1 at
``u_high``, hysteresis on the trailing mean, smoothing toward the schedule,
and a min-host plateau. Static mode is the same schedule with infinite
thresholds, so the boost stays at ``b_max``.

The trailing utilization is the 24 h mean of real-time utilization entering
the round. It does not include the round being priced.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pricing_core.money import D, dec_str


@dataclass(frozen=True)
class BoostState:
    applied: Decimal
    direction: int
    prev_trail: Decimal | None

    def as_dict(self) -> dict:
        return {
            "applied": dec_str(self.applied),
            "direction": int(self.direction),
            "prev_trail": None if self.prev_trail is None else dec_str(self.prev_trail),
        }


def conditional_floor_boost(
    utilization: Decimal,
    b_max: Decimal,
    u_low: Decimal,
    u_high: Decimal,
    *,
    hysteresis: Decimal = Decimal(0),
    direction: int = 0,
) -> Decimal:
    """Schedule value before smoothing.

    Infinite thresholds return ``b_max`` for every finite utilization. That
    is the static boost. A positive hysteresis shifts both ends in
    ``direction`` (+1 rising, −1 falling) so a small wiggle does not reverse
    the boost.
    """
    if b_max <= 0:
        raise ValueError("b_max must be positive")
    if hysteresis < 0:
        raise ValueError("boost hysteresis must be non-negative")
    if u_low.is_infinite() and u_high.is_infinite():
        return b_max
    if u_high < u_low:
        raise ValueError("u_high must be >= u_low")
    shift = hysteresis * D(int(direction))
    lo = u_low + shift
    hi = u_high + shift
    if hi <= lo:
        hi = lo + Decimal("1e-9")
    if utilization <= lo:
        return b_max
    if utilization >= hi:
        return Decimal(1)
    if u_high == u_low and hysteresis == 0:
        return b_max if utilization <= u_low else Decimal(1)
    weight = (utilization - lo) / (hi - lo)
    return b_max + (Decimal(1) - b_max) * weight


def step_boost(
    state: BoostState,
    trailing_util: Decimal,
    *,
    n_hosts: int,
    mode: str,
    b_max: Decimal,
    u_low: Decimal,
    u_high: Decimal,
    hysteresis: Decimal,
    smooth: Decimal,
    min_hosts: int,
    thin_plateau: bool,
) -> BoostState:
    """One boost step. Static mode holds ``b_max`` and does not smooth away.

    A conditional pool below ``min_hosts`` stays on ``b_max`` when
    ``thin_plateau`` is set. Smoothing walks the applied boost partway
    toward the schedule: ``applied + smooth × (schedule − applied)``.
    """
    if mode not in {"static", "conditional"}:
        raise ValueError("boost mode must be static or conditional")
    if not (Decimal(0) < smooth <= 1):
        raise ValueError("boost smooth must be in (0, 1]")
    if mode == "static":
        return BoostState(applied=b_max, direction=0, prev_trail=trailing_util)

    direction = int(state.direction)
    if state.prev_trail is not None:
        deadband = hysteresis * Decimal("0.5")
        if trailing_util > state.prev_trail + deadband:
            direction = 1
        elif trailing_util < state.prev_trail - deadband:
            direction = -1
    if thin_plateau and n_hosts < min_hosts:
        target = b_max
    else:
        target = conditional_floor_boost(
            trailing_util,
            b_max,
            u_low,
            u_high,
            hysteresis=hysteresis,
            direction=direction,
        )
    applied = state.applied + smooth * (target - state.applied)
    if applied <= 0:
        raise ValueError("applied boost became non-positive")
    return BoostState(applied=applied, direction=direction, prev_trail=trailing_util)
