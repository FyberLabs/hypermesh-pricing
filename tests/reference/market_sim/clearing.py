"""Real-time and day-ahead clearing.

Real-time (pricing-bid-process-2026-09-25.md §4): renter-proposing deferred
acceptance. Pools rank by share-cap band, tip, tier, then per-round lottery.
The loop moves rejected quantity strictly down the ladder and stops at a
fixed point or at ``max_passes`` (default 50).

Day-ahead (§4–5): sealed-bid uniform price per block, iterative and
downward-only. Clearing price is ``max(reserve, highest rejected in-cap bid)``.
Over-cap quantity does not set that price; it fills leftover supply only when
its bid is at least the uniform price, so a winner never pays above their bid.

Arrival order is not an input to either ranking. Lottery numbers are part of
the order. A colliding lottery falls through to ``order_id``, which is not
submission order.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from market_sim.orders import Order, capped_tip

EPS = 1e-9


@dataclass(frozen=True)
class PoolView:
    pool_id: str
    supply: float
    base: float
    reserve: float
    kind: str = "hardware"  # hardware | public_fallback


@dataclass
class Fill:
    order_id: str
    org_id: str
    pool_id: str
    rung_index: int
    quantity: float
    pay_per_hour: float
    willingness: float
    tip_applied: float
    kind: str


@dataclass
class PoolStats:
    pool_id: str
    supply: float
    demand: float
    utilization: float
    scarce: bool
    base: float
    reserve: float
    clearing_price: float | None
    kind: str


@dataclass
class ClearResult:
    fills: list[Fill]
    pools: dict[str, PoolStats]
    passes: int
    hit_iteration_cap: bool
    fallback_moves: int
    unfilled_quantity: float
    # order_id -> (rung_index of the best filled slice, or None)
    rung_won: dict[str, int | None] = field(default_factory=dict)
    final_price: dict[str, float | None] = field(default_factory=dict)


@dataclass
class _Chunk:
    chunk_id: int
    order_index: int
    order_id: str
    org_id: str
    lineage: int
    rung_index: int
    pool_id: str
    max_price: float
    willingness: float
    quantity: float
    tip: float
    tier_rank: int
    lottery: int
    kind: str


def _affordable(max_price: float, pool: PoolView, *, day_ahead: bool) -> bool:
    if pool.supply <= EPS:
        return False
    if max_price + EPS < pool.reserve:
        return False
    if day_ahead:
        return True
    return max_price + EPS >= pool.base


def _first_chunk(
    order: Order,
    order_index: int,
    pools: dict[str, PoolView],
    chunk_ids: list[int],
    *,
    day_ahead: bool,
) -> tuple[_Chunk | None, int]:
    """Place an order on its highest affordable rung. Returns skips made."""
    skips = 0
    for rung_index, rung in enumerate(order.rungs):
        pool = pools.get(rung.pool_id)
        if pool is None or not _affordable(rung.max_price, pool, day_ahead=day_ahead):
            skips += 1
            continue
        chunk_ids[0] += 1
        return (
            _Chunk(
                chunk_id=chunk_ids[0],
                order_index=order_index,
                order_id=order.order_id,
                org_id=order.org_id,
                lineage=order_index,
                rung_index=rung_index,
                pool_id=rung.pool_id,
                max_price=rung.max_price,
                willingness=rung.willingness,
                quantity=order.quantity,
                tip=order.tip,
                tier_rank=order.tier_rank,
                lottery=order.lottery,
                kind=order.kind,
            ),
            skips,
        )
    return None, skips


def _descend(
    chunk: _Chunk,
    orders: list[Order],
    pools: dict[str, PoolView],
    chunk_ids: list[int],
    *,
    day_ahead: bool,
) -> tuple[_Chunk | None, int]:
    order = orders[chunk.order_index]
    steps = 0
    rung_index = chunk.rung_index + 1
    while rung_index < len(order.rungs):
        steps += 1
        rung = order.rungs[rung_index]
        pool = pools.get(rung.pool_id)
        if pool is not None and _affordable(rung.max_price, pool, day_ahead=day_ahead):
            chunk_ids[0] += 1
            return (
                _Chunk(
                    chunk_id=chunk_ids[0],
                    order_index=chunk.order_index,
                    order_id=chunk.order_id,
                    org_id=chunk.org_id,
                    lineage=chunk.lineage,
                    rung_index=rung_index,
                    pool_id=rung.pool_id,
                    max_price=rung.max_price,
                    willingness=rung.willingness,
                    quantity=chunk.quantity,
                    tip=chunk.tip,
                    tier_rank=chunk.tier_rank,
                    lottery=chunk.lottery,
                    kind=chunk.kind,
                ),
                steps,
            )
        rung_index += 1
    return None, steps


def _split(chunk: _Chunk, keep_qty: float, chunk_ids: list[int]) -> tuple[_Chunk, _Chunk]:
    chunk_ids[0] += 1
    kept = _Chunk(
        chunk_id=chunk.chunk_id,
        order_index=chunk.order_index,
        order_id=chunk.order_id,
        org_id=chunk.org_id,
        lineage=chunk.lineage,
        rung_index=chunk.rung_index,
        pool_id=chunk.pool_id,
        max_price=chunk.max_price,
        willingness=chunk.willingness,
        quantity=keep_qty,
        tip=chunk.tip,
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
        max_price=chunk.max_price,
        willingness=chunk.willingness,
        quantity=chunk.quantity - keep_qty,
        tip=chunk.tip,
        tier_rank=chunk.tier_rank,
        lottery=chunk.lottery,
        kind=chunk.kind,
    )
    return kept, rest


def _allocate_realtime(
    chunks: list[_Chunk],
    supply: float,
    base: float,
    share_cap: float | None,
    tip_cap: float | None,
) -> dict[int, float]:
    """Greedy fill. In-cap slices outrank every over-cap slice."""
    total = sum(c.quantity for c in chunks)
    use_cap = share_cap is not None and total > supply + EPS
    cap_qty = float(share_cap) * float(supply) if use_cap else float("inf")
    by_org: dict[str, list[_Chunk]] = defaultdict(list)
    for chunk in chunks:
        by_org[chunk.org_id].append(chunk)

    slices: list[tuple[int, tuple, _Chunk, float]] = []
    for org_chunks in by_org.values():
        ranked = sorted(
            org_chunks,
            key=lambda c: (
                -capped_tip(c.tip, base, tip_cap),
                -c.tier_rank,
                c.lottery,
                c.order_id,
            ),
        )
        room = cap_qty
        for chunk in ranked:
            eff_tip = capped_tip(chunk.tip, base, tip_cap)
            key = (-eff_tip, -chunk.tier_rank, chunk.lottery, chunk.order_id)
            in_qty = min(chunk.quantity, max(0.0, room))
            over_qty = chunk.quantity - in_qty
            room -= in_qty
            if in_qty > EPS:
                slices.append((0, key, chunk, in_qty))
            if over_qty > EPS:
                slices.append((1, key, chunk, over_qty))
    slices.sort(key=lambda item: (item[0], item[1]))

    accepted: dict[int, float] = defaultdict(float)
    remaining = float(supply)
    for _band, _key, chunk, qty in slices:
        take = min(qty, remaining)
        if take > EPS:
            accepted[chunk.chunk_id] += take
            remaining -= take
    return accepted


def _allocate_day_ahead(
    chunks: list[_Chunk],
    supply: float,
    reserve: float,
    share_cap: float | None,
) -> tuple[dict[int, float], float]:
    """Uniform-price fill with a priority demotion above the share cap.

    Price = max(reserve, highest rejected *in-cap* bid). Over-cap rejections
    do not set the price. Over-cap slices fill leftover supply only if their
    bid is at least that price.
    """
    total = sum(c.quantity for c in chunks)
    use_cap = share_cap is not None and total > supply + EPS
    cap_qty = float(share_cap) * float(supply) if use_cap else float("inf")
    by_org: dict[str, list[_Chunk]] = defaultdict(list)
    for chunk in chunks:
        by_org[chunk.org_id].append(chunk)

    in_cap: list[tuple[tuple, _Chunk, float]] = []
    over_cap: list[tuple[tuple, _Chunk, float]] = []
    for org_chunks in by_org.values():
        ranked = sorted(
            org_chunks,
            key=lambda c: (-c.max_price, -c.tier_rank, c.lottery, c.order_id),
        )
        room = cap_qty
        for chunk in ranked:
            key = (-chunk.max_price, -chunk.tier_rank, chunk.lottery, chunk.order_id)
            in_qty = min(chunk.quantity, max(0.0, room))
            over_qty = chunk.quantity - in_qty
            room -= in_qty
            if in_qty > EPS:
                in_cap.append((key, chunk, in_qty))
            if over_qty > EPS:
                over_cap.append((key, chunk, over_qty))
    in_cap.sort(key=lambda item: item[0])
    over_cap.sort(key=lambda item: item[0])

    accepted: dict[int, float] = defaultdict(float)
    remaining = float(supply)
    rejected_in_cap_prices: list[float] = []
    for _key, chunk, qty in in_cap:
        take = min(qty, remaining)
        if take > EPS:
            accepted[chunk.chunk_id] += take
            remaining -= take
        if qty - take > EPS:
            rejected_in_cap_prices.append(chunk.max_price)

    if rejected_in_cap_prices:
        price = max(float(reserve), max(rejected_in_cap_prices))
    else:
        price = float(reserve)

    for _key, chunk, qty in over_cap:
        if chunk.max_price + EPS < price:
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
    share_cap: float | None = 0.25,
    tip_cap: float | None = 0.10,
    max_passes: int = 50,
) -> ClearResult:
    """Clear one real-time round at posted bases."""
    if max_passes < 1:
        raise ValueError("max_passes must be positive")
    chunk_ids = [0]
    seeking: list[_Chunk] = []
    fallback_moves = 0
    initial_qty = 0.0
    for index, order in enumerate(orders):
        initial_qty += order.quantity
        chunk, skips = _first_chunk(order, index, pools, chunk_ids, day_ahead=False)
        if chunk is not None:
            seeking.append(chunk)
            fallback_moves += skips

    seen: set[tuple[int, int]] = set()
    demand: dict[str, float] = defaultdict(float)
    passes = 0
    hit_cap = False
    placed: list[_Chunk] = []

    if seeking:
        for passes in range(1, max_passes + 1):
            for chunk in seeking:
                key = (chunk.lineage, chunk.rung_index)
                if key not in seen:
                    seen.add(key)
                    demand[chunk.pool_id] += chunk.quantity
            by_pool: dict[str, list[_Chunk]] = defaultdict(list)
            for chunk in placed + seeking:
                by_pool[chunk.pool_id].append(chunk)
            new_placed: list[_Chunk] = []
            rejected: list[_Chunk] = []
            for pool_id, chunks in by_pool.items():
                pool = pools[pool_id]
                accepted = _allocate_realtime(
                    chunks, pool.supply, pool.base, share_cap, tip_cap
                )
                for chunk in chunks:
                    got = accepted.get(chunk.chunk_id, 0.0)
                    if got >= chunk.quantity - EPS:
                        new_placed.append(chunk)
                    elif got > EPS:
                        kept, rest = _split(chunk, got, chunk_ids)
                        new_placed.append(kept)
                        rejected.append(rest)
                    else:
                        rejected.append(chunk)
            seeking = []
            for chunk in rejected:
                nxt, steps = _descend(chunk, orders, pools, chunk_ids, day_ahead=False)
                fallback_moves += steps if nxt is not None else max(0, steps)
                if nxt is not None:
                    seeking.append(nxt)
            placed = new_placed
            if not seeking:
                break
        else:
            hit_cap = True

    fills: list[Fill] = []
    filled_qty = 0.0
    rung_won: dict[str, int | None] = {order.order_id: None for order in orders}
    for chunk in placed:
        pool = pools[chunk.pool_id]
        pool_demand = demand.get(chunk.pool_id, 0.0)
        scarce = pool_demand > pool.supply + EPS
        tip_applied = capped_tip(chunk.tip, pool.base, tip_cap) if scarce else 0.0
        if scarce:
            pay = min(chunk.max_price, pool.base + tip_applied)
        else:
            pay = pool.base
        pay = max(pay, pool.reserve)
        fills.append(
            Fill(
                order_id=chunk.order_id,
                org_id=chunk.org_id,
                pool_id=chunk.pool_id,
                rung_index=chunk.rung_index,
                quantity=chunk.quantity,
                pay_per_hour=pay,
                willingness=chunk.willingness,
                tip_applied=tip_applied,
                kind=chunk.kind,
            )
        )
        filled_qty += chunk.quantity
        current = rung_won[chunk.order_id]
        if current is None or chunk.rung_index < current:
            rung_won[chunk.order_id] = chunk.rung_index

    stats: dict[str, PoolStats] = {}
    for pool_id, pool in pools.items():
        dem = demand.get(pool_id, 0.0)
        if pool.supply <= EPS:
            util = 0.0
        else:
            util = min(1.0, dem / pool.supply)
        stats[pool_id] = PoolStats(
            pool_id=pool_id,
            supply=pool.supply,
            demand=dem,
            utilization=util,
            scarce=dem > pool.supply + EPS,
            base=pool.base,
            reserve=pool.reserve,
            clearing_price=pool.base,
            kind=pool.kind,
        )
    return ClearResult(
        fills=fills,
        pools=stats,
        passes=passes,
        hit_iteration_cap=hit_cap,
        fallback_moves=fallback_moves,
        unfilled_quantity=max(0.0, initial_qty - filled_qty),
        rung_won=rung_won,
        final_price={pid: st.base for pid, st in stats.items()},
    )


def clear_day_ahead(
    orders: list[Order],
    pools: dict[str, PoolView],
    *,
    share_cap: float | None = 0.25,
    max_passes: int = 50,
) -> ClearResult:
    """Clear one day-ahead block. Movement is downward only."""
    if max_passes < 1:
        raise ValueError("max_passes must be positive")
    chunk_ids = [0]
    seeking: list[_Chunk] = []
    fallback_moves = 0
    initial_qty = 0.0
    for index, order in enumerate(orders):
        initial_qty += order.quantity
        chunk, skips = _first_chunk(order, index, pools, chunk_ids, day_ahead=True)
        if chunk is not None:
            seeking.append(chunk)
            fallback_moves += skips

    passes = 0
    hit_cap = False
    accepted_last: dict[str, dict[int, float]] = {}
    price_last: dict[str, float] = {}
    last_bid_qty: dict[str, float] = {}

    if seeking:
        for passes in range(1, max_passes + 1):
            by_pool: dict[str, list[_Chunk]] = defaultdict(list)
            for chunk in seeking:
                by_pool[chunk.pool_id].append(chunk)
            accepted_last = {}
            price_last = {}
            last_bid_qty = {
                pool_id: sum(chunk.quantity for chunk in chunks)
                for pool_id, chunks in by_pool.items()
            }
            next_seeking: list[_Chunk] = []
            moved = False
            rejected_parents: list[_Chunk] = []
            for pool_id, chunks in by_pool.items():
                pool = pools[pool_id]
                accepted, price = _allocate_day_ahead(
                    chunks, pool.supply, pool.reserve, share_cap
                )
                accepted_last[pool_id] = accepted
                price_last[pool_id] = price
                for chunk in chunks:
                    got = accepted.get(chunk.chunk_id, 0.0)
                    if got >= chunk.quantity - EPS:
                        next_seeking.append(chunk)
                    elif got > EPS:
                        kept, rest = _split(chunk, got, chunk_ids)
                        next_seeking.append(kept)
                        rejected_parents.append(rest)
                    else:
                        rejected_parents.append(chunk)
            for chunk in rejected_parents:
                nxt, steps = _descend(chunk, orders, pools, chunk_ids, day_ahead=True)
                if nxt is not None:
                    next_seeking.append(nxt)
                    fallback_moves += steps
                    moved = True
                else:
                    fallback_moves += steps
                    moved = moved or False
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

    # Fills are chunks seated on a rung and accepted on the last pass.
    # A chunk that descended on a capped final pass is not yet accepted.
    fills: list[Fill] = []
    filled_qty = 0.0
    rung_won: dict[str, int | None] = {order.order_id: None for order in orders}
    seated_by_pool: dict[str, list[_Chunk]] = defaultdict(list)
    for chunk in seeking:
        if passes and chunk.chunk_id not in accepted_ids:
            continue
        seated_by_pool[chunk.pool_id].append(chunk)

    for pool_id, chunks in seated_by_pool.items():
        price = price_last.get(pool_id, pools[pool_id].reserve)
        for chunk in chunks:
            pay = max(price, pools[pool_id].reserve)
            # Winners are only those accepted on the last pass. Chunks that
            # were moved in the same pass are not in `seeking` unless the loop
            # stopped early. On a stable pass, every seated chunk was accepted
            # in full. On a capped pass, a seated chunk might be a kept split
            # that was accepted. Rejected chunks are not seated.
            fills.append(
                Fill(
                    order_id=chunk.order_id,
                    org_id=chunk.org_id,
                    pool_id=chunk.pool_id,
                    rung_index=chunk.rung_index,
                    quantity=chunk.quantity,
                    pay_per_hour=pay,
                    willingness=chunk.willingness,
                    tip_applied=0.0,
                    kind="day_ahead",
                )
            )
            filled_qty += chunk.quantity
            current = rung_won[chunk.order_id]
            if current is None or chunk.rung_index < current:
                rung_won[chunk.order_id] = chunk.rung_index

    demand: dict[str, float] = defaultdict(float)
    for pool_id, qty in last_bid_qty.items():
        demand[pool_id] += qty

    stats: dict[str, PoolStats] = {}
    final_price: dict[str, float | None] = {}
    for pool_id, pool in pools.items():
        dem = demand.get(pool_id, 0.0)
        if pool_id in price_last:
            price = price_last[pool_id]
        elif pool.supply > EPS:
            # No surviving bids. Counterfactual lone-bidder price is reserve.
            price = pool.reserve
        else:
            price = None
        final_price[pool_id] = price
        if pool.supply <= EPS:
            util = 0.0
        else:
            util = min(1.0, dem / pool.supply)
        stats[pool_id] = PoolStats(
            pool_id=pool_id,
            supply=pool.supply,
            demand=dem,
            utilization=util,
            scarce=dem > pool.supply + EPS,
            base=pool.base,
            reserve=pool.reserve,
            clearing_price=price,
            kind=pool.kind,
        )
    return ClearResult(
        fills=fills,
        pools=stats,
        passes=passes,
        hit_iteration_cap=hit_cap,
        fallback_moves=fallback_moves,
        unfilled_quantity=max(0.0, initial_qty - filled_qty),
        rung_won=rung_won,
        final_price=final_price,
    )


def allocation_key(result: ClearResult) -> tuple:
    """Order-independent summary of who got what, at what price."""
    rows = [
        (
            fill.order_id,
            fill.pool_id,
            fill.rung_index,
            round(fill.quantity, 8),
            round(fill.pay_per_hour, 8),
        )
        for fill in result.fills
    ]
    return tuple(sorted(rows))


def org_quantity(result: ClearResult, pool_id: str) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for fill in result.fills:
        if fill.pool_id == pool_id:
            totals[fill.org_id] += fill.quantity
    return dict(totals)
