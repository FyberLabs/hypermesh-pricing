"""Token prices derived from the hourly reference.

pricing-floor-and-bidding-2026-09-25.md §4. Throughput is an argument.
This module does not contain a measured tokens/s figure.

    P_ref_hour = max(reserve, real-time base)
    price_per_1M = P_ref_hour / (3600 × tokens_per_second × u_batch) × 1e6

The cent quote is the ceiling, so a full hour at the supplied ``u_batch``
is never billed below the reference hour.
"""

from __future__ import annotations

from decimal import Decimal

from pricing_core.money import ceil_cents


def reference_cents(reserve_cents: int, base_cents: int) -> int:
    if reserve_cents < 0 or base_cents < 0:
        raise ValueError("prices must be non-negative")
    return max(reserve_cents, base_cents)


def price_per_thousand_cents(
    reference_cents_value: int,
    tokens_per_second: Decimal,
    u_batch: Decimal,
) -> int:
    """Ceiling cents per 1,000 tokens, so the implied hour stays at or above the reference.

    Per-1k is ``price_per_1M / 1000`` before the ceiling. Dynamic per-token
    clearing is not this function.
    """
    if tokens_per_second <= 0:
        raise ValueError("throughput parameter must be positive")
    if not (Decimal(0) < u_batch <= 1):
        raise ValueError("u_batch must be in (0, 1]")
    if reference_cents_value < 0:
        raise ValueError("reference price must be non-negative")
    tokens_per_hour = Decimal(3600) * tokens_per_second * u_batch
    dollars = (Decimal(reference_cents_value) / Decimal(100)) / tokens_per_hour * Decimal(1000)
    return ceil_cents(dollars)


def price_per_million_cents(
    reference_cents_value: int,
    tokens_per_second: Decimal,
    u_batch: Decimal,
) -> int:
    if tokens_per_second <= 0:
        raise ValueError("throughput parameter must be positive")
    if not (Decimal(0) < u_batch <= 1):
        raise ValueError("u_batch must be in (0, 1]")
    if reference_cents_value < 0:
        raise ValueError("reference price must be non-negative")
    tokens_per_hour = Decimal(3600) * tokens_per_second * u_batch
    dollars = (Decimal(reference_cents_value) / Decimal(100)) / tokens_per_hour * Decimal(1_000_000)
    return ceil_cents(dollars)


def implied_hourly_cents(
    price_per_million_cents_value: int,
    tokens_per_second: Decimal,
    u_batch: Decimal,
) -> int:
    """Revenue, in cents, from one box-hour at ``u_batch`` of the given rate.

    Uses the ceiled per-million price, so the result is at least the
    reference that produced it (within a cent of extra ceiling).
    """
    if tokens_per_second <= 0 or not (Decimal(0) < u_batch <= 1):
        raise ValueError("throughput parameters are invalid")
    tokens = Decimal(3600) * tokens_per_second * u_batch
    dollars = (Decimal(price_per_million_cents_value) / Decimal(100)) * tokens / Decimal(1_000_000)
    return ceil_cents(dollars)
