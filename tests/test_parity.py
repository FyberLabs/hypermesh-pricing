"""pricing_core matches the market-sim snapshot within one cent.

The snapshot under tests/reference is FyberLabs/market-sim commit
df06a32363342b0ef6b8753376686eb8bb8850bb. Figures are simulated.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from market_sim.clearing import PoolView as SimPool
from market_sim.clearing import clear_day_ahead as sim_day_ahead
from market_sim.clearing import clear_realtime as sim_realtime
from market_sim.controller import corridor_cap, step_base
from market_sim.floor import conditional_floor_boost as sim_boost
from market_sim.floor import floor_per_hour, reserve_per_hour
from market_sim.orders import Order as SimOrder
from market_sim.orders import Rung as SimRung
from market_sim.upgrade import commit_upgrade_pass as sim_upgrade

from pricing_core.boost import conditional_floor_boost
from pricing_core.clearing import PoolView, clear_day_ahead, clear_realtime, commit_upgrade_pass
from pricing_core.controller import corridor_cap_cents, step_base_cents
from pricing_core.engine import price_day_ahead, price_realtime
from pricing_core.floor import floor_dollars, reserve_dollars
from pricing_core.orders import Order, Rung

U_MIN = Decimal("0.30")
U_MAX = Decimal("0.70")


def _near(cents: int, dollars: float) -> None:
    assert abs(cents - dollars * 100) <= 1.0 + 1e-6


def _cents(dollars: float) -> int:
    return int((Decimal(str(dollars)) * 100).to_integral_value())


def _core_order(order: SimOrder) -> Order:
    return Order(
        order_id=order.order_id,
        org_id=order.org_id,
        rungs=tuple(
            Rung(
                pool_id=rung.pool_id,
                max_cents=_cents(rung.max_price),
                willingness_cents=_cents(rung.willingness),
            )
            for rung in order.rungs
        ),
        hours=Decimal(str(order.quantity)),
        tip_cents=_cents(order.tip),
        tier_rank=order.tier_rank,
        lottery=order.lottery,
        kind=order.kind,
    )


def _core_pools(pools: dict[str, SimPool]) -> dict[str, PoolView]:
    return {
        pid: PoolView(
            pid,
            supply=Decimal(str(pool.supply)),
            base_cents=_cents(pool.base),
            reserve_cents=_cents(pool.reserve),
        )
        for pid, pool in pools.items()
    }


def _sim_rung(pool: str, price: float) -> SimRung:
    return SimRung(pool, max_price=price, willingness=price)


def _sim_order(oid, org, rungs, tip=0.0, lottery=0, qty=1.0) -> SimOrder:
    return SimOrder(oid, org, tuple(rungs), quantity=qty, tip=tip, lottery=lottery)


def _sim_pools(**specs) -> dict[str, SimPool]:
    return {
        pid: SimPool(pid, supply=supply, base=base, reserve=reserve)
        for pid, (supply, base, reserve) in specs.items()
    }


def _assert_fills(core_fills, sim_fills) -> None:
    core = {(fill.order_id, fill.pool_id): fill for fill in core_fills}
    sim = {(fill.order_id, fill.pool_id): fill for fill in sim_fills}
    assert set(core) == set(sim)
    for key, sim_fill in sim.items():
        got = core[key]
        _near(got.pay_cents, sim_fill.pay_per_hour)
        assert abs(float(got.hours) - sim_fill.quantity) <= 1e-9
        assert got.rung_index == sim_fill.rung_index


def test_floor_matches_market_sim_within_a_cent():
    rows = [
        (0.070, 0.1834, 1.05, 1999),
        (0.80, 0.1834, 1.05, 4500),
        (1.275, 0.1419, 1.3, 35625),
        (130, 0.1419, 1.2, 3_000_000),
    ]
    for kw, tariff, overhead, capex in rows:
        sim_floor = floor_per_hour(kw, tariff, overhead, capex, 0.0, 0.50, 0.0)
        sim_reserve = reserve_per_hour(sim_floor, 0.08, 1.3)
        floor, _u = floor_dollars(
            Decimal(str(kw)),
            Decimal(str(tariff)),
            Decimal(str(overhead)),
            Decimal(capex),
            Decimal(0),
            Decimal("0.50"),
            Decimal(0),
            years=Decimal(3),
            hours_per_year=Decimal(8760),
            u_min=U_MIN,
            u_max=U_MAX,
        )
        reserve = reserve_dollars(floor, Decimal("0.08"), Decimal("1.3"))
        assert abs(float(floor) - sim_floor) < 1e-9
        assert abs(float(reserve) - sim_reserve) < 1e-9


def test_boost_schedule_matches_market_sim():
    for u in (0, 0.35, 0.40, 0.45, 0.55, 0.9, 1):
        sim = sim_boost(u, 1.3, 0.35, 0.55, hysteresis=0.03, direction=1)
        got = conditional_floor_boost(
            Decimal(str(u)),
            Decimal("1.3"),
            Decimal("0.35"),
            Decimal("0.55"),
            hysteresis=Decimal("0.03"),
            direction=1,
        )
        assert abs(float(got) - sim) < 1e-12


def test_controller_matches_market_sim_within_a_cent():
    cases = [
        (1.00, 1.0, 0.025, 0.50, 10.0),
        (0.40, 4 / 6, 0.10, 0.01, 10.0),
        (2.00, 0.0, 0.025, 1.00, 8.0),
        (1.00, 1.0, 0.025, 1.00, 3.00),
    ]
    for base, util, delta, reserve, cap in cases:
        sim = step_base(base, util, 0.70, delta, reserve, cap)
        got = step_base_cents(
            _cents(base),
            Decimal(str(util)),
            Decimal("0.70"),
            Decimal(str(delta)),
            _cents(reserve),
            _cents(cap),
        )
        _near(got, sim)
        assert got >= _cents(reserve)
    sim_cap = corridor_cap(1.0, [2.0, 3.0, 4.0], kappa=3, lambda_cap=1.5)
    # Median of 2, 3, 4 is 3. λ 1.5 → 4.5, above κ 3.
    got_cap = corridor_cap_cents(100, _cents(3.0), Decimal(3), Decimal("1.5"))
    _near(got_cap, sim_cap)


def test_illustrative_realtime_matches_market_sim():
    sim_orders = [
        _sim_order("A", "A", [_sim_rung("H", 1.50)], lottery=2),
        _sim_order("B", "B", [_sim_rung("H", 1.20), _sim_rung("L", 0.60)], lottery=3),
        _sim_order("C", "C", [_sim_rung("H", 1.10), _sim_rung("L", 0.50)], tip=0.05, lottery=11),
        _sim_order("D", "D", [_sim_rung("H", 0.90), _sim_rung("L", 0.50)], lottery=4),
        _sim_order("E", "E", [_sim_rung("H", 1.30)], tip=0.10, lottery=10),
        _sim_order("F", "F", [_sim_rung("H", 1.05), _sim_rung("L", 0.45)], lottery=1),
        _sim_order("G", "G", [_sim_rung("L", 0.70)], lottery=5),
        _sim_order("J", "J", [_sim_rung("L", 0.42)], lottery=6),
    ]
    sim_pools = _sim_pools(H=(4, 1.00, 0.01), L=(6, 0.40, 0.01))
    sim = sim_realtime(sim_orders, sim_pools, share_cap=0.25, tip_cap=0.10)
    core = clear_realtime(
        [_core_order(order) for order in sim_orders],
        _core_pools(sim_pools),
        share_cap=Decimal("0.25"),
        tip_cap=Decimal("0.10"),
    )
    _assert_fills(core.fills, sim.fills)
    assert core.passes == sim.passes
    assert core.pools["H"].scarce is sim.pools["H"].scarce


def test_day_ahead_and_upgrade_match_market_sim():
    sim_orders = [
        _sim_order("Big", "Big", [_sim_rung("X", 9.0)], lottery=1),
        _sim_order("S", "S", [_sim_rung("X", 5.0), _sim_rung("H", 5.0)], lottery=2),
        _sim_order("U", "U", [_sim_rung("H", 4.0), _sim_rung("L", 4.0)], lottery=3),
        _sim_order("Other", "Other", [_sim_rung("L", 2.0)], lottery=4),
    ]
    sim_pools = _sim_pools(X=(1, 1.0, 1.0), H=(1, 1.0, 1.0), L=(1, 1.0, 1.0))
    sim = sim_day_ahead(sim_orders, sim_pools, share_cap=None)
    core_orders = [_core_order(order) for order in sim_orders]
    core_pools = _core_pools(sim_pools)
    core = clear_day_ahead(core_orders, core_pools, share_cap=None)
    _assert_fills(core.fills, sim.fills)
    sim_up, sim_gains = sim_upgrade(sim_orders, sim, sim_pools, None)
    core_up, core_gains = commit_upgrade_pass(core_orders, core, core_pools, None)
    assert core_gains == sim_gains
    _assert_fills(core_up.fills, sim_up.fills)


def test_engine_scenarios_match_market_sim_on_seeded_books():
    # Two seeded books: the memo's illustrative real-time round, and a
    # day-ahead block with the one-pass upgrade. Prices are exact cents.
    realtime = price_realtime(
        {
            "round_id": "rt-5547",
            "round_start": "2026-09-25T00:15:00Z",
            "pools": [
                _engine_pool("H", supply="4", base=100, reserve=50, hosts=8, trail="0.80"),
                _engine_pool("L", supply="6", base=40, reserve=20, hosts=8, trail="0.50"),
            ],
            "orders": [
                _engine_order("A", "A", [("H", 150)], lottery=2),
                _engine_order("B", "B", [("H", 120), ("L", 60)], lottery=3),
                _engine_order("C", "C", [("H", 110), ("L", 50)], tip=5, lottery=11),
                _engine_order("D", "D", [("H", 90), ("L", 50)], lottery=4),
                _engine_order("E", "E", [("H", 130)], tip=10, lottery=10),
                _engine_order("F", "F", [("H", 105), ("L", 45)], lottery=1),
                _engine_order("G", "G", [("L", 70)], lottery=5),
                _engine_order("J", "J", [("L", 42)], lottery=6),
            ],
        }
    )
    sim_orders = [
        _sim_order("A", "A", [_sim_rung("H", 1.50)], lottery=2),
        _sim_order("B", "B", [_sim_rung("H", 1.20), _sim_rung("L", 0.60)], lottery=3),
        _sim_order("C", "C", [_sim_rung("H", 1.10), _sim_rung("L", 0.50)], tip=0.05, lottery=11),
        _sim_order("D", "D", [_sim_rung("H", 0.90), _sim_rung("L", 0.50)], lottery=4),
        _sim_order("E", "E", [_sim_rung("H", 1.30)], tip=0.10, lottery=10),
        _sim_order("F", "F", [_sim_rung("H", 1.05), _sim_rung("L", 0.45)], lottery=1),
        _sim_order("G", "G", [_sim_rung("L", 0.70)], lottery=5),
        _sim_order("J", "J", [_sim_rung("L", 0.42)], lottery=6),
    ]
    sim = sim_realtime(
        sim_orders,
        _sim_pools(H=(4, 1.00, 0.50), L=(6, 0.40, 0.20)),
        share_cap=0.25,
        tip_cap=0.10,
    )
    pays = {fill["order_id"]: fill for fill in realtime["fills"]}
    for fill in sim.fills:
        got = pays[fill.order_id]
        assert got["pool_id"] == fill.pool_id
        _near(got["pay_cents"], fill.pay_per_hour)
    again = price_realtime(
        {
            "round_id": "rt-5547",
            "round_start": "2026-09-25T00:15:00Z",
            "pools": list(reversed([
                _engine_pool("H", supply="4", base=100, reserve=50, hosts=8, trail="0.80"),
                _engine_pool("L", supply="6", base=40, reserve=20, hosts=8, trail="0.50"),
            ])),
            "orders": list(reversed([
                _engine_order("A", "A", [("H", 150)], lottery=2),
                _engine_order("B", "B", [("H", 120), ("L", 60)], lottery=3),
                _engine_order("C", "C", [("H", 110), ("L", 50)], tip=5, lottery=11),
                _engine_order("D", "D", [("H", 90), ("L", 50)], lottery=4),
                _engine_order("E", "E", [("H", 130)], tip=10, lottery=10),
                _engine_order("F", "F", [("H", 105), ("L", 45)], lottery=1),
                _engine_order("G", "G", [("L", 70)], lottery=5),
                _engine_order("J", "J", [("L", 42)], lottery=6),
            ])),
        }
    )
    assert again["fills"] == realtime["fills"]
    assert again["pools"] == realtime["pools"]

    ahead = price_day_ahead(
        {
            "round_id": "da-5547",
            "round_start": "2026-09-25T00:00:00Z",
            "pools": [
                _engine_pool("X", supply="1", base=100, reserve=100, hosts=4, trail="0.20", offered="4"),
                _engine_pool("H", supply="1", base=100, reserve=100, hosts=4, trail="0.20", offered="4"),
                _engine_pool("L", supply="1", base=100, reserve=100, hosts=4, trail="0.20", offered="4"),
            ],
            "orders": [
                _engine_order("Big", "Big", [("X", 900)], lottery=1),
                _engine_order("S", "S", [("X", 500), ("H", 500)], lottery=2),
                _engine_order("U", "U", [("H", 400), ("L", 400)], lottery=3),
                _engine_order("Other", "Other", [("L", 200)], lottery=4),
            ],
        }
    )
    # A 25% cap on a 1-hour pool splits a 1-hour bid, so this request does
    # not reproduce the simulator's share_cap=None upgrade story. That story
    # is test_day_ahead_and_upgrade_match_market_sim. Here the engine must
    # still stamp the ruleset, keep every price at or above the reserve, and
    # run the upgrade pass.
    assert ahead["upgrade_applied"] is True
    assert all(fill["pay_cents"] >= 100 for fill in ahead["fills"])
    assert ahead["ruleset_version"] == "2026-09-25.1"
    assert ahead["engine_version"] == "0.1.0"


def _engine_pool(pid, *, supply, base, reserve, hosts, trail, offered=None):
    body = {
        "pool_id": pid,
        "class_id": "class",
        "region": "us-east",
        "supply_hours": supply,
        "n_hosts": hosts,
        "reserve_cents": reserve,
        "prev_base_cents": base,
        "trailing_util_24h": trail,
        "day_ahead_median_7d_cents": None,
        "boost": {"applied": "1.3", "direction": 0, "prev_trail": None},
    }
    if offered is not None:
        body["offered_hours"] = offered
    return body


def _engine_order(oid, org, rungs, *, tip=0, lottery=0, hours="1"):
    return {
        "order_id": oid,
        "org_id": org,
        "lottery": lottery,
        "tip_cents": tip,
        "hours": hours,
        "rungs": [{"pool_id": pool, "max_cents": cents} for pool, cents in rungs],
    }


def test_day_ahead_share_is_clamped_at_seventy_five_percent():
    result = price_day_ahead(
        {
            "round_id": "da-cap",
            "pools": [
                _engine_pool("H", supply="90", base=100, reserve=100, hosts=10, trail="0.4", offered="100"),
            ],
            "orders": [_engine_order("A", "A", [("H", 500)], hours="80", lottery=1)],
        }
    )
    pool = result["pools"][0]
    assert pool["supply_clamped"] is True
    assert pool["supply_hours"] == "75"
    assert pool["base_cents"] >= pool["reserve_cents"]
    assert pool["next_base_cents"] <= pool["cap_cents"]
    assert pool["next_base_cents"] >= pool["reserve_cents"]
