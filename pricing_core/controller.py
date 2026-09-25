"""Real-time base-price controller.

pricing-bid-process-2026-09-25.md §1.

    U = min(1, D / S)
    e = (U − U*) / U*          if U < U*
        (U − U*) / (1 − U*)    if U ≥ U*
    base' = clamp(base × (1 + δ e), reserve, cap)
    cap   = max(κ × reserve, λ × trailing day-ahead median)

Prices are integer cents. The multiplicative step is ``Decimal``; the cent
is half-even, then clamped so the result is never below the reserve and
never above the cap. A cap that would sit under the reserve becomes the
reserve.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN

from pricing_core.money import at_least_reserve, clamp_int


def demand_utilization(demand: Decimal, supply: Decimal) -> Decimal:
    if supply <= 0:
        return Decimal(0)
    if demand < 0:
        raise ValueError("demand must be non-negative")
    return min(Decimal(1), demand / supply)


def normalized_error(utilization: Decimal, target: Decimal) -> Decimal:
    if not (Decimal(0) < target < 1):
        raise ValueError("target utilization must be in (0, 1)")
    u = min(Decimal(1), max(Decimal(0), utilization))
    if u < target:
        return (u - target) / target
    return (u - target) / (Decimal(1) - target)


def corridor_cap_cents(
    reserve_cents: int,
    day_ahead_median_cents: int | None,
    kappa: Decimal,
    lambda_shock: Decimal,
) -> int:
    """Soft ceiling in cents: max(κ × reserve, λ × day-ahead median)."""
    if reserve_cents < 0:
        raise ValueError("reserve must be non-negative")
    if kappa < 0 or lambda_shock < 0:
        raise ValueError("kappa and lambda must be non-negative")
    cap = _half_even(Decimal(reserve_cents) * kappa)
    if day_ahead_median_cents is not None:
        if day_ahead_median_cents < 0:
            raise ValueError("day-ahead median must be non-negative")
        shocked = _half_even(Decimal(day_ahead_median_cents) * lambda_shock)
        cap = max(cap, shocked)
    return at_least_reserve(cap, reserve_cents)


def step_base_cents(
    base_cents: int,
    utilization: Decimal,
    target: Decimal,
    delta: Decimal,
    reserve_cents: int,
    cap_cents: int,
) -> int:
    """One controller step in integer cents."""
    if delta < 0:
        raise ValueError("delta must be non-negative")
    if base_cents < 0 or reserve_cents < 0:
        raise ValueError("prices must be non-negative")
    error = normalized_error(utilization, target)
    raw = Decimal(base_cents) * (Decimal(1) + delta * error)
    stepped = _half_even(raw)
    return clamp_int(stepped, reserve_cents, max(cap_cents, reserve_cents))


def update_base_cents(
    base_cents: int,
    demand: Decimal,
    supply: Decimal,
    n_hosts: int,
    *,
    target: Decimal,
    delta: Decimal,
    reserve_cents: int,
    cap_cents: int,
    min_hosts: int,
) -> tuple[int, Decimal, bool]:
    """Return ``(next_base_cents, utilization, updated)``.

    Pools with fewer than ``min_hosts`` hosts are pinned at the reserve.
    A round with no supply leaves the base unchanged, still clamped up to
    the reserve, unless the pool is thin.
    """
    if n_hosts < min_hosts:
        return reserve_cents, demand_utilization(demand, supply), False
    if supply <= 0:
        return at_least_reserve(base_cents, reserve_cents), Decimal(0), False
    util = demand_utilization(demand, supply)
    nxt = step_base_cents(base_cents, util, target, delta, reserve_cents, cap_cents)
    return nxt, util, True


def update_ema_cents(previous_cents: int, observation_cents: int, alpha: Decimal) -> int:
    """EMA of a cent price. Half-even, then not below zero."""
    if not (Decimal(0) < alpha <= 1):
        raise ValueError("EMA alpha must be in (0, 1]")
    raw = alpha * Decimal(observation_cents) + (Decimal(1) - alpha) * Decimal(previous_cents)
    return max(0, _half_even(raw))


def _half_even(amount: Decimal) -> int:
    if amount < 0:
        raise ValueError("price became negative")
    return int(amount.to_integral_value(rounding=ROUND_HALF_EVEN))
