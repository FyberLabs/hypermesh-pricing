"""Real-time base-price controller.

Implements pricing-bid-process-2026-09-25.md §1.

    U_t = min(1, D_t / S_t)
    e_t = (U − U*) / U*            if U < U*
          (U − U*) / (1 − U*)      if U ≥ U*
    base_{t+1} = clamp(base_t × (1 + δ e_t), reserve, cap_t)
    cap_t = max(κ × reserve, λ × median trailing day-ahead price)
"""

from __future__ import annotations

import math


def demand_utilization(demand: float, supply: float) -> float:
    """Utilization pinned at 1 when demand meets or exceeds supply.

    Supply of 0 is undefined; callers should skip the update. This returns 0
    so a stray call does not raise inside a metric.
    """
    if supply <= 0:
        return 0.0
    if demand < 0:
        raise ValueError("demand must be non-negative")
    return min(1.0, float(demand) / float(supply))


def normalized_error(utilization: float, target: float) -> float:
    """Asymmetric error in [-1, 1] at the extremes, 0 at the target."""
    if not 0 < target < 1:
        raise ValueError("target utilization U* must be in (0, 1)")
    u = min(1.0, max(0.0, float(utilization)))
    if u < target:
        return (u - target) / target
    return (u - target) / (1.0 - target)


def corridor_cap(
    reserve: float,
    day_ahead_prices: list[float] | tuple[float, ...],
    kappa: float,
    lambda_cap: float,
) -> float:
    """Soft ceiling: max(κ × reserve, λ × trailing day-ahead median)."""
    if reserve < 0:
        raise ValueError("reserve must be non-negative")
    if kappa < 0 or lambda_cap < 0:
        raise ValueError("kappa and lambda must be non-negative")
    cap = float(kappa) * float(reserve)
    if day_ahead_prices:
        median = _median(day_ahead_prices)
        cap = max(cap, float(lambda_cap) * median)
    return cap


def step_base(
    base: float,
    utilization: float,
    target: float,
    delta: float,
    reserve: float,
    cap: float,
) -> float:
    """One EIP-1559-style step, clamped to [reserve, cap].

    The hard floor wins if a caller passes a cap below reserve.
    """
    if delta < 0:
        raise ValueError("delta must be non-negative")
    if base < 0 or reserve < 0:
        raise ValueError("prices must be non-negative")
    error = normalized_error(utilization, target)
    raw = float(base) * (1.0 + float(delta) * error)
    lo = float(reserve)
    hi = float(cap)
    if hi < lo:
        return lo
    return min(max(raw, lo), hi)


def update_base(
    base: float,
    demand: float,
    supply: float,
    n_hosts: int,
    *,
    target: float,
    delta: float,
    reserve: float,
    cap: float,
    thin_pool_hosts: int,
) -> tuple[float, float, bool]:
    """Return ``(new_base, utilization, updated)``.

    Pools with fewer than ``N`` hosts are pinned at reserve. A round with no
    offered supply leaves the base unchanged (there is no utilization signal),
    unless the pool is thin, in which case it stays pinned.
    """
    if n_hosts < thin_pool_hosts:
        return float(reserve), demand_utilization(demand, supply), False
    if supply <= 0:
        return float(base), 0.0, False
    util = demand_utilization(demand, supply)
    return (
        step_base(base, util, target, delta, reserve, cap),
        util,
        True,
    )


def update_ema(previous: float, observation: float, alpha: float) -> float:
    """Exponential moving average. ``alpha`` is the weight on the new sample."""
    if not 0 < alpha <= 1:
        raise ValueError("EMA alpha must be in (0, 1]")
    return float(alpha) * float(observation) + (1.0 - float(alpha)) * float(previous)


def _median(values: list[float] | tuple[float, ...]) -> float:
    ordered = sorted(float(v) for v in values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def compound_bound(delta: float, steps: int) -> float:
    """Largest multiplicative rise over ``steps`` full-up rounds: (1+δ)^steps."""
    if steps < 0:
        raise ValueError("steps must be non-negative")
    return math.pow(1.0 + float(delta), steps)
