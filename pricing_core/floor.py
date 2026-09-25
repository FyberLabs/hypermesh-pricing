"""Hourly floor and reserve.

pricing-floor-and-bidding-2026-09-25.md §1. The platform fee ``take`` is a
required argument. This module does not choose 8% or 12%.

    floor_hour   = energy_hour + depreciation_hour + host_margin_hour
    energy_hour  = P_loaded_kW × tariff × overhead
    depreciation = (capex − residual) / (years × hours_per_year × u)
    reserve_hour = floor_hour × boost / (1 − take)

Integer cents are the ceiling of the exact ``Decimal`` result, so a cent
quote is never below the reserve the formula produced.
"""

from __future__ import annotations

from decimal import Decimal

from pricing_core.money import D, ceil_cents


def clamp_utilization(u: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    if hi < lo:
        raise ValueError("utilization band is inverted")
    return min(hi, max(lo, u))


def energy_per_hour(loaded_kw: Decimal, tariff_per_kwh: Decimal, overhead: Decimal) -> Decimal:
    if loaded_kw < 0 or tariff_per_kwh < 0 or overhead < 0:
        raise ValueError("energy inputs must be non-negative")
    return loaded_kw * tariff_per_kwh * overhead


def depreciation_per_hour(
    capex: Decimal,
    residual: Decimal,
    utilization: Decimal,
    *,
    years: Decimal,
    hours_per_year: Decimal,
    u_min: Decimal,
    u_max: Decimal,
    clamp: bool = True,
) -> Decimal:
    if years <= 0 or hours_per_year <= 0:
        raise ValueError("depreciation horizon must be positive")
    if capex < residual:
        raise ValueError("residual cannot exceed capex")
    u = clamp_utilization(utilization, u_min, u_max) if clamp else utilization
    if u <= 0:
        raise ValueError("utilization must be positive")
    return (capex - residual) / (years * hours_per_year * u)


def floor_dollars(
    loaded_kw: Decimal,
    tariff_per_kwh: Decimal,
    overhead: Decimal,
    capex: Decimal,
    residual: Decimal,
    utilization: Decimal,
    host_margin: Decimal,
    *,
    years: Decimal,
    hours_per_year: Decimal,
    u_min: Decimal,
    u_max: Decimal,
    clamp_u: bool = True,
) -> tuple[Decimal, Decimal]:
    """Return ``(floor, utilization used)`` in dollars and a fraction."""
    if host_margin < 0:
        raise ValueError("host margin must be >= 0")
    u_used = clamp_utilization(utilization, u_min, u_max) if clamp_u else utilization
    energy = energy_per_hour(loaded_kw, tariff_per_kwh, overhead)
    depr = depreciation_per_hour(
        capex,
        residual,
        utilization,
        years=years,
        hours_per_year=hours_per_year,
        u_min=u_min,
        u_max=u_max,
        clamp=clamp_u,
    )
    return energy + depr + host_margin, u_used


def reserve_dollars(floor: Decimal, take: Decimal, boost: Decimal = Decimal(1)) -> Decimal:
    """Gross-up so the host nets ``floor × boost`` after ``take``.

    ``reserve = floor × boost / (1 − take)``. Open question 14 in the floor
    memo; the formula in §1 is the gross-up, which is what this package
    implements. ``take`` has no default.
    """
    if floor < 0:
        raise ValueError("floor must be non-negative")
    if not (Decimal(0) <= take < 1):
        raise ValueError("take must be in [0, 1)")
    if boost <= 0:
        raise ValueError("boost must be positive")
    return floor * boost / (Decimal(1) - take)


def reserve_cents(floor: Decimal, take: Decimal, boost: Decimal = Decimal(1)) -> int:
    """Integer cents, ceiling, so the quote is never below the exact reserve."""
    return ceil_cents(reserve_dollars(floor, take, boost))


def floor_cents_from_dollars(floor: Decimal) -> int:
    return ceil_cents(floor)


def equivalent_depreciation_utilization(utilization: Decimal, boost: Decimal) -> Decimal:
    """Utilization ``u/b`` that matches scaling a depreciation-only floor by ``b``."""
    if utilization <= 0:
        raise ValueError("utilization must be positive")
    if boost <= 0:
        raise ValueError("boost must be positive")
    return utilization / boost


def quote_floor(
    *,
    loaded_kw: Decimal,
    tariff_per_kwh: Decimal,
    overhead: Decimal,
    capex: Decimal,
    residual: Decimal,
    utilization: Decimal,
    host_margin: Decimal,
    take: Decimal,
    boost: Decimal,
    years: int,
    hours_per_year: int,
    u_min: Decimal,
    u_max: Decimal,
) -> dict:
    """Boundary quote: exact decimals plus ceiling cents."""
    floor, u_used = floor_dollars(
        loaded_kw,
        tariff_per_kwh,
        overhead,
        capex,
        residual,
        utilization,
        host_margin,
        years=D(years),
        hours_per_year=D(hours_per_year),
        u_min=u_min,
        u_max=u_max,
    )
    reserve = reserve_dollars(floor, take, boost)
    return {
        "floor": floor,
        "reserve": reserve,
        "u_used": u_used,
        "floor_cents": ceil_cents(floor),
        "reserve_cents": ceil_cents(reserve),
    }
