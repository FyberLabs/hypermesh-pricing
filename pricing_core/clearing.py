"""Real-time and day-ahead clearing.

Real-time (pricing-bid-process-2026-09-25.md §4): renter-proposing deferred
acceptance at a posted base. Pools rank by share-cap band, tip, tier, then
lottery (lower wins). A duplicated lottery falls through to ``order_id``,
which is lexicographic and is not submission order. Everyone in a pool that
is not scarce pays the posted base. A scarce pool adds that renter's own
capped tip, never above their max. That posted base is the uniform price;
the tip is the memo's scarce-round exception.

Day-ahead (§4–5): sealed-bid uniform price, downward-only, then one upgrade
pass when the ruleset says so. The uniform price is
``max(reserve, highest rejected in-cap bid)``. Over-cap quantity does not
set the price. It fills leftover supply only when its bid is at least the
uniform price, so a winner never pays above their bid.

Money is integer cents. Hours are ``Decimal``. Identical inputs, including
lottery numbers, produce identical fills.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from pricing_core.orders import Order, capped_tip_cents

EPS = Decimal("1e-9")


@dataclass(frozen=True)
class PoolView:
    pool_id: str
    supply: Decimal
    base_cents: int
    reserve_cents: int


@dataclass
class Fill:
    order_id: str
    org_id: str
    pool_id: str
    rung_index: int
    hours: Decimal
    pay_cents: int
    tip_cents: int
    kind: str


@dataclass
class PoolStats:
    pool_id: str
    supply: Decimal
    demand: Decimal
    utilization: Decimal
    scarce: bool
    base_cents: int
    reserve_cents: int
    clearing_cents: int | None


@dataclass
class ClearResult:
    fills: list[Fill]
    pools: dict[str, PoolStats]
    passes: int
    hit_iteration_cap: bool
    unfilled_hours: Decimal
    rung_won: dict[str, int | None] = field(default_factory=dict)
    final_price_cents: dict[str, int | None] = field(default_factory=dict)


@dataclass
class _Chunk:
    chunk_id: int
    order_index: int
    order_id: str
    org_id: str
    lineage: int
    rung_index: int
    pool_id: str
    max_cents: int
    willingness_cents: int
    hours: Decimal
    tip_cents: int
    tier_rank: int
    lottery: int
    kind: str


def _affordable(max_cents: int, pool: PoolView, *, day_ahead: bool) -> bool:
    if pool.supply <= EPS:
        return False
    if max_cents < pool.reserve_cents:
        return False
    if day_ahead:
        return True
    return max_cents >= pool.base_cents


def _first_chunk(
    order: Order,
    order_index: int,
    pools: dict[str, PoolView],
    chunk_ids: list[int],
    *,
    day_ahead: bool,
) -> _Chunk | None:
    for rung_index, rung in enumerate(order.rungs):
        pool = pools.get(rung.pool_id)
        if pool is None or not _affordable(rung.max_cents, pool, day_ahead=day_ahead):
            continue
        chunk_ids[0] += 1
        return _Chunk(
            chunk_id=chunk_ids[0],
            order_index=order_index,
            order_id=order.order_id,
            org_id=order.org_id,
            lineage=order_index,
            rung_index=rung_index,
            pool_id=rung.pool_id,
            max_cents=rung.max_cents,
            willingness_cents=rung.willingness_cents,
            hours=order.hours,
            tip_cents=order.tip_cents,
            tier_rank=order.tier_rank,
            lottery=order.lottery,
            kind=order.kind,
        )
    return None


def _descend(
    chunk: _Chunk,
    orders: list[Order],
    pools: dict[str, PoolView],
    chunk_ids: list[int],
    *,
    day_ahead: bool,
) -> _Chunk | None:
    order = orders[chunk.order_index]
    rung_index = chunk.rung_index + 1
    while rung_index < len(order.rungs):
        rung = order.rungs[rung_index]
        pool = pools.get(rung.pool_id)
        if pool is not None and _affordable(rung.max_cents, pool, day_ahead=day_ahead):
            chunk_ids[0] += 1
            return _Chunk(
                chunk_id=chunk_ids[0],
                order_index=chunk.order_index,
                order_id=chunk.order_id,
                org_id=chunk.org_id,
                lineage=chunk.lineage,
                rung_index=rung_index,
                pool_id=rung.pool_id,
                max_cents=rung.max_cents,
                willingness_cents=rung.willingness_cents,
                hours=chunk.hours,
                tip_cents=chunk.tip_cents,
                tier_rank=chunk.tier_rank,
                lottery=chunk.lottery,
                kind=chunk.kind,
            )
        rung_index += 1
    return None


def _split(chunk: _Chunk, keep_hours: Decimal, chunk_ids: list[int]) -> tuple[_Chunk, _Chunk]:
    kept = _Chunk(
        chunk_id=chunk.chunk_id,
        order_index=chunk.order_index,
        order_id=chunk.order_id,
        org_id=chunk.org_id,
        lineage=chunk.lineage,
        rung_index=chunk.rung_index,
        pool_id=chunk.pool_id,
        max_cents=chunk.max_cents,
        willingness_cents=chunk.willingness_cents,
        hours=keep_hours,
        tip_cents=chunk.tip_cents,
        tier_rank=chunk.tier_rank,
        lottery=chunk.lottery,
        kind=chunk.kind,
    )
    chunk_ids[0] += 1
    rest = _Chunk(
        chunk_id=chunk_ids[0],
        order_index=chunk.order_index,
        order_id=chunk.order_id,
        org_id=chunk.org_id,
        lineage=chunk.lineage,
        rung_index=chunk.rung_index,
        pool_id=chunk.pool_id,
        max_cents=chunk.max_cents,
        willingness_cents=chunk.willingness_cents,
        hours=chunk.hours - keep_hours,
        tip_cents=chunk.tip_cents,
        tier_rank=chunk.tier_rank,
        lottery=chunk.lottery,
        kind=chunk.kind,
    )
    return kept, rest


def _rank_key_realtime(chunk: _Chunk, base_cents: int, tip_cap: Decimal | None) -> tuple:
    tip = capped_tip_cents(chunk.tip_cents, base_cents, tip_cap)
    return (-tip, -chunk.tier_rank, chunk.lottery, chunk.order_id)


def _allocate_realtime(
    chunks: list[_Chunk],
    supply: Decimal,
    base_cents: int,
    share_cap: Decimal | None,
    tip_cap: Decimal | None,
) -> dict[int, Decimal]:
    """Greedy fill. In-cap slices outrank every over-cap slice."""
    total = sum((c.hours for c in chunks), Decimal(0))
    use_cap = share_cap is not None and total > supply + EPS
    cap_hours = share_cap * supply if use_cap else Decimal("Infinity")
    by_org: dict[str, list[_Chunk]] = defaultdict(list)
    for chunk in chunks:
        by_org[chunk.org_id].append(chunk)

    slices: list[tuple[int, tuple, _Chunk, Decimal]] = []
    for org_chunks in by_org.values():
        ranked = sorted(org_chunks, key=lambda c: _rank_key_realtime(c, base_cents, tip_cap))
        room = cap_hours
        for chunk in ranked:
            key = _rank_key_realtime(chunk, base_cents, tip_cap)
            in_qty = min(chunk.hours, max(Decimal(0), room))
            over_qty = chunk.hours - in_qty
            room -= in_qty
            if in_qty > EPS:
                slices.append((0, key, chunk, in_qty))
            if over_qty > EPS:
                slices.append((1, key, chunk, over_qty))
    slices.sort(key=lambda item: (item[0], item[1]))

    accepted: dict[int, Decimal] = defaultdict(lambda: Decimal(0))
    remaining = supply
    for _band, _key, chunk, qty in slices:
        take = min(qty, remaining)
        if take > EPS:
            accepted[chunk.chunk_id] += take
            remaining -= take
    return accepted


def _allocate_day_ahead(
    chunks: list[_Chunk],
    supply: Decimal,
    reserve_cents: int,
    share_cap: Decimal | None,
) -> tuple[dict[int, Decimal], int]:
    total = sum((c.hours for c in chunks), Decimal(0))
    use_cap = share_cap is not None and total > supply + EPS
    cap_hours = share_cap * supply if use_cap else Decimal("Infinity")
    by_org: dict[str, list[_Chunk]] = defaultdict(list)
    for chunk in chunks:
        by_org[chunk.org_id].append(chunk)

    in_cap: list[tuple[tuple, _Chunk, Decimal]] = []
    over_cap: list[tuple[tuple, _Chunk, Decimal]] = []
    for org_chunks in by_org.values():
        ranked = sorted(
            org_chunks,
            key=lambda c: (-c.max_cents, -c.tier_rank, c.lottery, c.order_id),
        )
        room = cap_hours
        for chunk in ranked:
            key = (-chunk.max_cents, -chunk.tier_rank, chunk.lottery, chunk.order_id)
            in_qty = min(chunk.hours, max(Decimal(0), room))
            over_qty = chunk.hours - in_qty
            room -= in_qty
            if in_qty > EPS:
                in_cap.append((key, chunk, in_qty))
            if over_qty > EPS:
                over_cap.append((key, chunk, over_qty))
    in_cap.sort(key=lambda item: item[0])
    over_cap.sort(key=lambda item: item[0])

    accepted: dict[int, Decimal] = defaultdict(lambda: Decimal(0))
    remaining = supply
    rejected_prices: list[int] = []
    for _key, chunk, qty in in_cap:
        take = min(qty, remaining)
        if take > EPS:
            accepted[chunk.chunk_id] += take
            remaining -= take
        if qty - take > EPS:
            rejected_prices.append(chunk.max_cents)

    price = max(reserve_cents, max(rejected_prices)) if rejected_prices else reserve_cents

    for _key, chunk, qty in over_cap:
        if chunk.max_cents < price:
            continue
        take = min(qty, remaining)
        if take > EPS:
            accepted[chunk.chunk_id] += take
            remaining -= take
    return accepted, price


def clear_realtime(
    orders: list[Order],
    pools: dict[str, PoolView],
    *,
    share_cap: Decimal | None = Decimal("0.25"),
    tip_cap: Decimal | None = Decimal("0.10"),
    max_passes: int = 50,
) -> ClearResult:
    """Clear one real-time round at posted bases."""
    if max_passes < 1:
        raise ValueError("max_passes must be positive")
    return _clear(orders, pools, share_cap=share_cap, tip_cap=tip_cap, max_passes=max_passes, day_ahead=False)


def clear_day_ahead(
    orders: list[Order],
    pools: dict[str, PoolView],
    *,
    share_cap: Decimal | None = Decimal("0.25"),
    max_passes: int = 50,
) -> ClearResult:
    """Clear one day-ahead block. Movement in this call is downward only."""
    if max_passes < 1:
        raise ValueError("max_passes must be positive")
    return _clear(orders, pools, share_cap=share_cap, tip_cap=None, max_passes=max_passes, day_ahead=True)


def _clear(
    orders: list[Order],
    pools: dict[str, PoolView],
    *,
    share_cap: Decimal | None,
    tip_cap: Decimal | None,
    max_passes: int,
    day_ahead: bool,
) -> ClearResult:
    chunk_ids = [0]
    # Stable processing order. Ranking itself does not use arrival order.
    ordered = sorted(enumerate(orders), key=lambda item: (item[1].lottery, item[1].order_id))
    index_of = {order.order_id: index for index, order in enumerate(orders)}
    seeking: list[_Chunk] = []
    initial = sum((order.hours for order in orders), Decimal(0))
    for _sort_key, order in ordered:
        chunk = _first_chunk(order, index_of[order.order_id], pools, chunk_ids, day_ahead=day_ahead)
        if chunk is not None:
            seeking.append(chunk)

    if day_ahead:
        return _finish_day_ahead(orders, pools, seeking, chunk_ids, share_cap, max_passes, initial)
    return _finish_realtime(orders, pools, seeking, chunk_ids, share_cap, tip_cap, max_passes, initial)


def _finish_realtime(
    orders: list[Order],
    pools: dict[str, PoolView],
    seeking: list[_Chunk],
    chunk_ids: list[int],
    share_cap: Decimal | None,
    tip_cap: Decimal | None,
    max_passes: int,
    initial: Decimal,
) -> ClearResult:
    seen: set[tuple[int, int]] = set()
    demand: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    passes = 0
    hit_cap = False
    placed: list[_Chunk] = []
    if seeking:
        for passes in range(1, max_passes + 1):
            for chunk in seeking:
                key = (chunk.lineage, chunk.rung_index)
                if key not in seen:
                    seen.add(key)
                    demand[chunk.pool_id] += chunk.hours
            by_pool: dict[str, list[_Chunk]] = defaultdict(list)
            for chunk in placed + seeking:
                by_pool[chunk.pool_id].append(chunk)
            new_placed: list[_Chunk] = []
            rejected: list[_Chunk] = []
            for pool_id in sorted(by_pool):
                pool = pools[pool_id]
                accepted = _allocate_realtime(
                    by_pool[pool_id], pool.supply, pool.base_cents, share_cap, tip_cap
                )
                for chunk in by_pool[pool_id]:
                    got = accepted.get(chunk.chunk_id, Decimal(0))
                    if got >= chunk.hours - EPS:
                        new_placed.append(chunk)
                    elif got > EPS:
                        kept, rest = _split(chunk, got, chunk_ids)
                        new_placed.append(kept)
                        rejected.append(rest)
                    else:
                        rejected.append(chunk)
            seeking = []
            for chunk in rejected:
                nxt = _descend(chunk, orders, pools, chunk_ids, day_ahead=False)
                if nxt is not None:
                    seeking.append(nxt)
            placed = new_placed
            if not seeking:
                break
        else:
            hit_cap = True

    fills: list[Fill] = []
    filled = Decimal(0)
    rung_won: dict[str, int | None] = {order.order_id: None for order in orders}
    for chunk in placed:
        pool = pools[chunk.pool_id]
        scarce = demand.get(chunk.pool_id, Decimal(0)) > pool.supply + EPS
        tip = capped_tip_cents(chunk.tip_cents, pool.base_cents, tip_cap) if scarce else 0
        pay = min(chunk.max_cents, pool.base_cents + tip) if scarce else pool.base_cents
        pay = max(pay, pool.reserve_cents)
        if pay > chunk.max_cents:
            continue
        fills.append(
            Fill(
                order_id=chunk.order_id,
                org_id=chunk.org_id,
                pool_id=chunk.pool_id,
                rung_index=chunk.rung_index,
                hours=chunk.hours,
                pay_cents=pay,
                tip_cents=tip,
                kind=chunk.kind,
            )
        )
        filled += chunk.hours
        current = rung_won[chunk.order_id]
        if current is None or chunk.rung_index < current:
            rung_won[chunk.order_id] = chunk.rung_index
    stats = _stats(pools, demand, day_ahead=False, prices=None)
    return ClearResult(
        fills=_sort_fills(fills),
        pools=stats,
        passes=passes,
        hit_iteration_cap=hit_cap,
        unfilled_hours=max(Decimal(0), initial - filled),
        rung_won=rung_won,
        final_price_cents={pid: pool.base_cents for pid, pool in pools.items()},
    )


def _finish_day_ahead(
    orders: list[Order],
    pools: dict[str, PoolView],
    seeking: list[_Chunk],
    chunk_ids: list[int],
    share_cap: Decimal | None,
    max_passes: int,
    initial: Decimal,
) -> ClearResult:
    passes = 0
    hit_cap = False
    accepted_last: dict[str, dict[int, Decimal]] = {}
    price_last: dict[str, int] = {}
    last_bid: dict[str, Decimal] = {}
    if seeking:
        for passes in range(1, max_passes + 1):
            by_pool: dict[str, list[_Chunk]] = defaultdict(list)
            for chunk in seeking:
                by_pool[chunk.pool_id].append(chunk)
            accepted_last = {}
            price_last = {}
            last_bid = {
                pool_id: sum((chunk.hours for chunk in chunks), Decimal(0))
                for pool_id, chunks in by_pool.items()
            }
            next_seeking: list[_Chunk] = []
            moved = False
            rejected_parents: list[_Chunk] = []
            for pool_id in sorted(by_pool):
                pool = pools[pool_id]
                accepted, price = _allocate_day_ahead(
                    by_pool[pool_id], pool.supply, pool.reserve_cents, share_cap
                )
                accepted_last[pool_id] = accepted
                price_last[pool_id] = price
                for chunk in by_pool[pool_id]:
                    got = accepted.get(chunk.chunk_id, Decimal(0))
                    if got >= chunk.hours - EPS:
                        next_seeking.append(chunk)
                    elif got > EPS:
                        kept, rest = _split(chunk, got, chunk_ids)
                        next_seeking.append(kept)
                        rejected_parents.append(rest)
                    else:
                        rejected_parents.append(chunk)
            for chunk in rejected_parents:
                nxt = _descend(chunk, orders, pools, chunk_ids, day_ahead=True)
                if nxt is not None:
                    next_seeking.append(nxt)
                    moved = True
            seeking = next_seeking
            if not moved:
                break
        else:
            hit_cap = True

    accepted_ids: set[int] = set()
    for accepted in accepted_last.values():
        for chunk_id, qty in accepted.items():
            if qty > EPS:
                accepted_ids.add(chunk_id)

    fills: list[Fill] = []
    filled = Decimal(0)
    rung_won: dict[str, int | None] = {order.order_id: None for order in orders}
    for chunk in seeking:
        if passes and chunk.chunk_id not in accepted_ids:
            continue
        price = price_last.get(chunk.pool_id, pools[chunk.pool_id].reserve_cents)
        pay = max(price, pools[chunk.pool_id].reserve_cents)
        pay = min(pay, chunk.max_cents)
        if pay < pools[chunk.pool_id].reserve_cents:
            continue
        fills.append(
            Fill(
                order_id=chunk.order_id,
                org_id=chunk.org_id,
                pool_id=chunk.pool_id,
                rung_index=chunk.rung_index,
                hours=chunk.hours,
                pay_cents=pay,
                tip_cents=0,
                kind="day_ahead",
            )
        )
        filled += chunk.hours
        current = rung_won[chunk.order_id]
        if current is None or chunk.rung_index < current:
            rung_won[chunk.order_id] = chunk.rung_index

    final: dict[str, int | None] = {}
    for pool_id, pool in pools.items():
        if pool_id in price_last:
            final[pool_id] = price_last[pool_id]
        elif pool.supply > EPS:
            final[pool_id] = pool.reserve_cents
        else:
            final[pool_id] = None
    stats = _stats(pools, last_bid, day_ahead=True, prices=final)
    return ClearResult(
        fills=_sort_fills(fills),
        pools=stats,
        passes=passes,
        hit_iteration_cap=hit_cap,
        unfilled_hours=max(Decimal(0), initial - filled),
        rung_won=rung_won,
        final_price_cents=final,
    )


def commit_upgrade_pass(
    orders: list[Order],
    result: ClearResult,
    pools: dict[str, PoolView],
    share_cap: Decimal | None,
) -> tuple[ClearResult, int]:
    """One re-clear. Stayers keep their rung; a renter who can afford a higher
    rung at the final prices bids that rung. Rung indexes refer to the
    original ladder.
    """
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
            price = result.final_price_cents.get(rung.pool_id)
            if price is not None and rung.willingness_cents >= price:
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
                hours=order.hours,
                tip_cents=0,
                tier_rank=order.tier_rank,
                lottery=order.lottery,
                kind="day_ahead",
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
    diagnostic.fills = _sort_fills(diagnostic.fills)
    return diagnostic, gains


def _stats(
    pools: dict[str, PoolView],
    demand: dict[str, Decimal],
    *,
    day_ahead: bool,
    prices: dict[str, int | None] | None,
) -> dict[str, PoolStats]:
    stats: dict[str, PoolStats] = {}
    for pool_id in sorted(pools):
        pool = pools[pool_id]
        dem = demand.get(pool_id, Decimal(0))
        util = Decimal(0) if pool.supply <= EPS else min(Decimal(1), dem / pool.supply)
        price: int | None
        if prices is None:
            price = pool.base_cents
        else:
            price = prices.get(pool_id)
        stats[pool_id] = PoolStats(
            pool_id=pool_id,
            supply=pool.supply,
            demand=dem,
            utilization=util,
            scarce=dem > pool.supply + EPS,
            base_cents=pool.base_cents,
            reserve_cents=pool.reserve_cents,
            clearing_cents=price,
        )
    return stats


def _sort_fills(fills: list[Fill]) -> list[Fill]:
    return sorted(fills, key=lambda fill: (fill.pool_id, fill.order_id, fill.rung_index))


def org_hours(result: ClearResult, pool_id: str) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for fill in result.fills:
        if fill.pool_id == pool_id:
            totals[fill.org_id] += fill.hours
    return dict(totals)
