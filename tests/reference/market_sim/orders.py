"""Renter orders, pain levels, and fallback ladders.

Implements pricing-bid-process-2026-09-25.md §2–3.

Pain thresholds are applied *before* clearing. Clearing sees only an effective
max and the remaining ladder. Notify never changes those inputs.

Interpretation (see docs/memo-mapping.md): thresholds are compared to the
lagged EMA of the pool base. Auto-fallback skips the rung until the EMA is
below the threshold by the hysteresis margin. Confirm lowers the effective
max until the renter confirms. Stop drops the rest of the ladder.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PainState:
    """Per-renter pain memory. Mutated as thresholds fire across rounds."""

    fallen_back: dict[int, bool] = field(default_factory=dict)
    confirmed: bool = False
    notify_count: int = 0


@dataclass(frozen=True)
class Rung:
    pool_id: str
    max_price: float
    willingness: float
    auto_fallback: float | None = None
    confirm: float | None = None
    stop: float | None = None
    notify: float | None = None


@dataclass(frozen=True)
class Order:
    """One prepaid order. ``lottery`` is drawn once per round, not at arrival."""

    order_id: str
    org_id: str
    rungs: tuple[Rung, ...]
    quantity: float
    tip: float = 0.0
    tier_rank: int = 0
    lottery: int = 0
    kind: str = "realtime"  # realtime or day_ahead
    cohort: str = ""

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise ValueError("quantity must be non-negative")
        if self.tip < 0:
            raise ValueError("tip must be non-negative")
        if not self.rungs:
            raise ValueError("order needs at least one rung")


def capped_tip(tip: float, base: float, tip_cap: float | None) -> float:
    """Apply the per-round tip cap.

    ``tip_cap is None`` leaves the tip uncapped. ``tip_cap == 0`` disables tips.
    A positive cap is a fraction of the posted base (the memo starts at 10%).
    """
    if tip < 0 or base < 0:
        raise ValueError("tip and base must be non-negative")
    if tip_cap is None:
        return float(tip)
    if tip_cap <= 0:
        return 0.0
    return min(float(tip), float(tip_cap) * float(base))


def apply_pain(
    order: Order,
    ema_by_pool: dict[str, float],
    state: PainState,
    *,
    hysteresis: float = 0.05,
) -> Order | None:
    """Return the order clearing should see, or None if the renter has stopped.

    Uses the EMA carried into the round (the caller does not include the
    current base). Hysteresis is multiplicative: return only when
    ``ema <= threshold × (1 − hysteresis)``.
    """
    if hysteresis < 0:
        raise ValueError("hysteresis must be non-negative")
    kept: list[Rung] = []
    for index, rung in enumerate(order.rungs):
        ema = float(ema_by_pool.get(rung.pool_id, 0.0))
        if rung.notify is not None and ema >= rung.notify:
            state.notify_count += 1
        if rung.stop is not None and ema >= rung.stop:
            break
        if rung.auto_fallback is not None:
            level = float(rung.auto_fallback)
            if state.fallen_back.get(index, False):
                release = level * (1.0 - hysteresis)
                if ema <= release:
                    state.fallen_back[index] = False
                else:
                    continue
            elif ema >= level:
                state.fallen_back[index] = True
                continue
        effective = float(rung.max_price)
        if rung.confirm is not None and ema >= float(rung.confirm) and not state.confirmed:
            effective = min(effective, float(rung.confirm))
        kept.append(
            Rung(
                pool_id=rung.pool_id,
                max_price=effective,
                willingness=rung.willingness,
                auto_fallback=None,
                confirm=None,
                stop=None,
                notify=None,
            )
        )
    if not kept:
        return None
    return Order(
        order_id=order.order_id,
        org_id=order.org_id,
        rungs=tuple(kept),
        quantity=order.quantity,
        tip=order.tip,
        tier_rank=order.tier_rank,
        lottery=order.lottery,
        kind=order.kind,
        cohort=order.cohort,
    )
