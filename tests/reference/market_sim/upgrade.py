"""Upgrade-pass helpers copied from market-sim metrics.py.

Snapshot of FyberLabs/market-sim commit df06a32363342b0ef6b8753376686eb8bb8850bb.
Only the day-ahead upgrade functions are included. The rest of metrics.py
depends on numpy and pandas and is not part of the parity fixture.
"""

from __future__ import annotations

from market_sim.clearing import ClearResult, PoolView, clear_day_ahead
from market_sim.orders import Order


def price_taker_rung_loss(orders: list[Order], result: ClearResult) -> dict[str, int]:
    comparable = 0
    gaps = 0
    downshifts = 0
    for order in orders:
        won = result.rung_won.get(order.order_id)
        best: int | None = None
        for index, rung in enumerate(order.rungs):
            price = result.final_price.get(rung.pool_id)
            if price is None:
                continue
            if rung.willingness + 1e-9 >= price:
                best = index
                break
        if best is None:
            continue
        comparable += 1
        if won is None or best < won:
            gaps += 1
            if won is not None and best < won:
                downshifts += 1
    return {"comparable": comparable, "gaps": gaps, "downshifts": downshifts}


def commit_upgrade_pass(
    orders: list[Order],
    result: ClearResult,
    pools: dict[str, PoolView],
    share_cap: float | None,
) -> tuple[ClearResult, int]:
    rebids: list[Order] = []
    chosen_index: dict[str, int] = {}
    attempting: set[str] = set()
    for order in orders:
        won = result.rung_won.get(order.order_id)
        current = won if won is not None else len(order.rungs)
        best: int | None = None
        for index, rung in enumerate(order.rungs):
            if index >= current:
                break
            price = result.final_price.get(rung.pool_id)
            if price is not None and rung.willingness + 1e-9 >= price:
                best = index
                break
        if best is not None:
            chosen = best
            attempting.add(order.order_id)
        elif won is not None:
            chosen = won
        else:
            continue
        chosen_index[order.order_id] = chosen
        rebids.append(
            Order(
                order_id=order.order_id,
                org_id=order.org_id,
                rungs=(order.rungs[chosen],),
                quantity=order.quantity,
                tip=0.0,
                tier_rank=order.tier_rank,
                lottery=order.lottery,
                kind="day_ahead",
                cohort=order.cohort,
            )
        )
    if not rebids or not attempting:
        return result, 0
    diagnostic = clear_day_ahead(rebids, pools, share_cap=share_cap, max_passes=1)
    gains = 0
    remapped: dict[str, int | None] = {order.order_id: None for order in orders}
    for order_id, won in diagnostic.rung_won.items():
        if won is None or order_id not in chosen_index:
            remapped[order_id] = None
            continue
        remapped[order_id] = chosen_index[order_id]
        if order_id in attempting:
            gains += 1
    for fill in diagnostic.fills:
        if fill.order_id in chosen_index:
            fill.rung_index = chosen_index[fill.order_id]
    diagnostic.rung_won = remapped
    return diagnostic, gains
