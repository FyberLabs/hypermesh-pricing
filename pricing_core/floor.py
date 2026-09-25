"""Hourly floor and reserve.

pricing-floor-and-bidding-2026-09-25.md §1. The platform fee ``take`` is a
required argument. This module does not choose 8% or 12%.

    floor_hour   = energy_hour + depreciation_hour + host_margin_hour
    energy_hour  = P_loaded_kW × tariff × overhead
    depreciation = (capex − residual) / (years × hours_per_year × u)
    reserve_hour = (floor_hour × boost + processor_fixed_per_hour) / (1 − take − processor_pct)

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


def reserve_dollars(
    floor: Decimal,
    take: Decimal,
    boost: Decimal = Decimal(1),
    *,
    processor_pct: Decimal = Decimal(0),
    processor_fixed_per_hour: Decimal = Decimal(0),
) -> Decimal:
    """Gross-up so the host nets ``floor × boost`` after take and processor fees.

    ``reserve = (floor × boost + processor_fixed_per_hour) / (1 − take − processor_pct)``.

    ``processor_fixed_per_hour`` is the per-transaction fixed fee spread over
    ``expected_hours_per_txn`` (see ``fixed_fee_per_hour``). Both processor
    inputs default to zero, which is the gross-up ``floor × boost / (1 − take)``.
    ``take`` has no default. Neither fee is a measured rate.
    """
    if floor < 0:
        raise ValueError("floor must be non-negative")
    if processor_fixed_per_hour < 0:
        raise ValueError("processor fixed fee must be non-negative")
    if not (Decimal(0) <= take < 1):
        raise ValueError("take must be in [0, 1)")
    if not (Decimal(0) <= processor_pct < 1):
        raise ValueError("processor_pct must be in [0, 1)")
    if boost <= 0:
        raise ValueError("boost must be positive")
    denominator = Decimal(1) - take - processor_pct
    if denominator <= 0:
        raise ValueError("take plus processor_pct must be < 1")
    return (floor * boost + processor_fixed_per_hour) / denominator


def fixed_fee_per_hour(processor_fixed_cents: int, expected_hours_per_txn: Decimal | None) -> Decimal:
    """Spread a per-transaction fixed fee across the hours that transaction covers.

    A zero fixed fee does not need ``expected_hours_per_txn``. A positive fee
    does, and the hours must be positive. The result is dollars per hour.
    """
    if isinstance(processor_fixed_cents, bool) or not isinstance(processor_fixed_cents, int):
        raise TypeError("processor_fixed_cents must be an int")
    if processor_fixed_cents < 0:
        raise ValueError("processor_fixed_cents must be >= 0")
    if processor_fixed_cents == 0:
        return Decimal(0)
    if expected_hours_per_txn is None or expected_hours_per_txn <= 0:
        raise ValueError("expected_hours_per_txn is required when processor_fixed_cents is positive")
    return (Decimal(processor_fixed_cents) / Decimal(100)) / expected_hours_per_txn


def reserve_cents(
    floor: Decimal,
    take: Decimal,
    boost: Decimal = Decimal(1),
    *,
    processor_pct: Decimal = Decimal(0),
    processor_fixed_per_hour: Decimal = Decimal(0),
) -> int:
    """Integer cents, ceiling, so the quote is never below the exact reserve."""
    return ceil_cents(
        reserve_dollars(
            floor,
            take,
            boost,
            processor_pct=processor_pct,
            processor_fixed_per_hour=processor_fixed_per_hour,
        )
    )


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
    processor_pct: Decimal = Decimal(0),
    processor_fixed_per_hour: Decimal = Decimal(0),
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
    reserve = reserve_dollars(
        floor,
        take,
        boost,
        processor_pct=processor_pct,
        processor_fixed_per_hour=processor_fixed_per_hour,
    )
    return {
        "floor": floor,
        "reserve": reserve,
        "u_used": u_used,
        "floor_cents": ceil_cents(floor),
        "reserve_cents": ceil_cents(reserve),
    }
