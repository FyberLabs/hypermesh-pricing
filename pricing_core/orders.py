"""Renter orders, the tip cap, and pain levels.

pricing-bid-process-2026-09-25.md §2–3. Clearing sees an effective max and
the remaining ladder. Notify increments a counter and does not change the
max. Thresholds are compared to the lagged EMA of the pool base, in cents.

The tip cap is a fraction of the posted base. The allowed tip is floored,
so it never exceeds that fraction.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

from pricing_core.money import floor_div_cents


@dataclass(frozen=True)
class Rung:
    pool_id: str
    max_cents: int
    willingness_cents: int
    auto_fallback_cents: int | None = None
    confirm_cents: int | None = None
    stop_cents: int | None = None
    notify_cents: int | None = None


@dataclass(frozen=True)
class Order:
    order_id: str
    org_id: str
    rungs: tuple[Rung, ...]
    hours: Decimal
    tip_cents: int = 0
    tier_rank: int = 0
    lottery: int = 0
    kind: str = "realtime"

    def __post_init__(self) -> None:
        if self.hours < 0:
            raise ValueError("hours must be non-negative")
        if self.tip_cents < 0:
            raise ValueError("tip must be non-negative")
        if not self.rungs:
            raise ValueError("order needs at least one rung")


@dataclass(frozen=True)
class PainState:
    fallen_back: dict[int, bool]
    confirmed: bool = False
    notify_count: int = 0

    def as_dict(self) -> dict:
        return {
            "fallen_back": {str(index): flag for index, flag in sorted(self.fallen_back.items()) if flag},
            "confirmed": self.confirmed,
            "notify_count": self.notify_count,
        }


def capped_tip_cents(tip_cents: int, base_cents: int, tip_cap: Decimal | None) -> int:
    """Tip actually used for priority and for payment, in cents."""
    if tip_cents < 0 or base_cents < 0:
        raise ValueError("tip and base must be non-negative")
    if tip_cap is None:
        return tip_cents
    if tip_cap <= 0:
        return 0
    allowed = floor_div_cents(Decimal(base_cents) * tip_cap)
    return min(tip_cents, allowed)


def apply_pain(
    order: Order,
    ema_cents_by_pool: dict[str, int],
    state: PainState,
    *,
    hysteresis: Decimal,
) -> tuple[Order | None, PainState]:
    """Return the order clearing should see, plus the updated pain memory.

    Hysteresis is multiplicative: a fallen-back rung returns only when
    ``ema <= threshold × (1 − hysteresis)``. The returned state is new;
    ``state`` is not mutated.
    """
    if hysteresis < 0:
        raise ValueError("hysteresis must be non-negative")
    fallen = dict(state.fallen_back)
    confirmed = state.confirmed
    notify_count = state.notify_count
    kept: list[Rung] = []
    for index, rung in enumerate(order.rungs):
        ema = int(ema_cents_by_pool.get(rung.pool_id, 0))
        if rung.notify_cents is not None and ema >= rung.notify_cents:
            notify_count += 1
        if rung.stop_cents is not None and ema >= rung.stop_cents:
            break
        if rung.auto_fallback_cents is not None:
            level = Decimal(rung.auto_fallback_cents)
            if fallen.get(index, False):
                release = int((level * (Decimal(1) - hysteresis)).to_integral_value(rounding=ROUND_FLOOR))
                if ema <= release:
                    fallen[index] = False
                else:
                    continue
            elif ema >= rung.auto_fallback_cents:
                fallen[index] = True
                continue
        effective = rung.max_cents
        if rung.confirm_cents is not None and ema >= rung.confirm_cents and not confirmed:
            effective = min(effective, rung.confirm_cents)
        kept.append(
            Rung(
                pool_id=rung.pool_id,
                max_cents=effective,
                willingness_cents=rung.willingness_cents,
            )
        )
    new_state = PainState(fallen_back=fallen, confirmed=confirmed, notify_count=notify_count)
    if not kept:
        return None, new_state
    return (
        Order(
            order_id=order.order_id,
            org_id=order.org_id,
            rungs=tuple(kept),
            hours=order.hours,
            tip_cents=order.tip_cents,
            tier_rank=order.tier_rank,
            lottery=order.lottery,
            kind=order.kind,
        ),
        new_state,
    )
