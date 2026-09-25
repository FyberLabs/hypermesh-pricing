"""Invariants ported from market-sim, on the cent boundary."""

from __future__ import annotations

from decimal import Decimal

import pytest

from pricing_core.boost import BoostState, conditional_floor_boost, step_boost
from pricing_core.clearing import PoolView, clear_day_ahead, clear_realtime, commit_upgrade_pass, org_hours
from pricing_core.controller import corridor_cap_cents, demand_utilization, normalized_error, step_base_cents, update_base_cents
from pricing_core.floor import floor_cents_from_dollars, floor_dollars, reserve_cents, reserve_dollars
from pricing_core.orders import Order, PainState, Rung, apply_pain, capped_tip_cents
from pricing_core.token_pricing import implied_hourly_cents, price_per_million_cents, reference_cents

U_MIN = Decimal("0.30")
U_MAX = Decimal("0.70")
YEARS = Decimal(3)
HOURS = Decimal(8760)


def _floor(kw, tariff, overhead, capex, u="0.50"):
    floor, _used = floor_dollars(
        Decimal(kw),
        Decimal(tariff),
        Decimal(overhead),
        Decimal(capex),
        Decimal(0),
        Decimal(u),
        Decimal(0),
        years=YEARS,
        hours_per_year=HOURS,
        u_min=U_MIN,
        u_max=U_MAX,
    )
    return floor


def _rung(pool: str, cents: int) -> Rung:
    return Rung(pool, max_cents=cents, willingness_cents=cents)


def _order(oid, org, rungs, tip=0, lottery=0, hours="1", tier=0) -> Order:
    return Order(oid, org, tuple(rungs), hours=Decimal(hours), tip_cents=tip, tier_rank=tier, lottery=lottery)


def _pools(**specs) -> dict[str, PoolView]:
    return {
        pid: PoolView(pid, supply=Decimal(supply), base_cents=base, reserve_cents=reserve)
        for pid, (supply, base, reserve) in specs.items()
    }


# Published rounded figures from the floor memo §2. Planning inputs, not measured.
PUBLISHED = [
    ("jetson", "0.070", "0.1834", "1.05", "1999", Decimal("0.166"), Decimal("0.180")),
    ("seeed", "0.050", "0.1834", "1.05", "1502.99", Decimal("0.124"), Decimal("0.135")),
    ("rtx", "0.80", "0.1834", "1.05", "4500", Decimal("0.497"), Decimal("0.540")),
    ("ryzen", "0.16", "0.1834", "1.05", "3449", Decimal("0.293"), Decimal("0.319")),
    ("mac", "0.14", "0.1834", "1.05", "1799", Decimal("0.164"), Decimal("0.178")),
    ("h100", "1.275", "0.1419", "1.3", "35625", Decimal("2.95"), Decimal("3.20")),
    ("b200", "1.788", "0.1419", "1.3", "64375", Decimal("5.23"), Decimal("5.68")),
    ("gb200", "130", "0.1419", "1.2", "3000000", Decimal("250.45"), Decimal("272.23")),
]


@pytest.mark.parametrize("row", PUBLISHED, ids=[row[0] for row in PUBLISHED])
def test_published_floor_ceils_and_stays_within_a_cent(row):
    _name, kw, tariff, overhead, capex, published_floor, published_reserve = row
    floor = _floor(kw, tariff, overhead, capex)
    reserve = reserve_dollars(floor, Decimal("0.08"), Decimal(1))
    floor_cents = floor_cents_from_dollars(floor)
    got_reserve = reserve_cents(floor, Decimal("0.08"), Decimal(1))
    assert Decimal(floor_cents) / 100 >= floor
    assert Decimal(got_reserve) / 100 >= reserve
    assert abs(floor_cents - published_floor * 100) <= 1
    assert abs(got_reserve - published_reserve * 100) <= 1
    assert got_reserve == reserve_cents(floor, Decimal("0.08"))


def test_utilization_band_and_full_model_take():
    low = _floor("0.07", "0.1834", "1.05", "1999", "0.30")
    high = _floor("0.07", "0.1834", "1.05", "1999", "0.70")
    assert low > high
    floor = _floor("0.07", "0.1834", "1.05", "1999")
    # The gross-up is strictly higher at 12% than at 8%. On a sub-dollar
    # floor the ceiling to cents can land on the same cent.
    assert reserve_dollars(floor, Decimal("0.12")) > reserve_dollars(floor, Decimal("0.08"))
    assert reserve_cents(floor, Decimal("0.12")) >= reserve_cents(floor, Decimal("0.08"))


def test_asymmetric_error_and_illustrative_step():
    assert normalized_error(Decimal(0), Decimal("0.70")) == Decimal("-1")
    assert normalized_error(Decimal(1), Decimal("0.70")) == Decimal(1)
    assert normalized_error(Decimal("0.70"), Decimal("0.70")) == 0
    # Bid memo §7 made-up numbers, δ = 0.10 as in that illustration.
    assert demand_utilization(Decimal(5), Decimal(4)) == 1
    nxt = step_base_cents(100, Decimal(1), Decimal("0.70"), Decimal("0.10"), reserve_cents=1, cap_cents=1000)
    assert nxt == 110
    base = 100
    cap = 400
    for _ in range(40):
        base = step_base_cents(base, Decimal(1), Decimal("0.70"), Decimal("0.10"), 100, cap)
        assert 100 <= base <= cap
    assert base == cap


def test_empty_demand_walks_down_to_the_reserve_and_not_past_it():
    base = step_base_cents(200, Decimal(0), Decimal("0.70"), Decimal("0.10"), 100, 800)
    assert base == 180
    for _ in range(40):
        base = step_base_cents(base, Decimal(0), Decimal("0.70"), Decimal("0.10"), 100, 800)
        assert base >= 100
    assert base == 100


def test_corridor_and_thin_pool():
    # κ = 3, reserve 100 → 300. Median 200, λ = 1.5 → 300. Equal.
    assert corridor_cap_cents(100, 400, Decimal(3), Decimal("1.5")) == 600
    assert corridor_cap_cents(100, None, Decimal(3), Decimal("1.5")) == 300
    nxt, _util, updated = update_base_cents(
        500, Decimal(10), Decimal(2), n_hosts=2,
        target=Decimal("0.70"), delta=Decimal("0.025"),
        reserve_cents=150, cap_cents=2000, min_hosts=3,
    )
    assert nxt == 150
    assert updated is False
    held, util, updated = update_base_cents(
        200, Decimal(0), Decimal(0), n_hosts=5,
        target=Decimal("0.70"), delta=Decimal("0.025"),
        reserve_cents=100, cap_cents=800, min_hosts=3,
    )
    assert held == 200
    assert util == 0
    assert updated is False


def test_boost_plateaus_fade_hysteresis_and_thin_plateau():
    assert conditional_floor_boost(Decimal("0.20"), Decimal("1.3"), Decimal("0.35"), Decimal("0.55")) == Decimal("1.3")
    assert conditional_floor_boost(Decimal("0.35"), Decimal("1.3"), Decimal("0.35"), Decimal("0.55")) == Decimal("1.3")
    assert conditional_floor_boost(Decimal("0.55"), Decimal("1.3"), Decimal("0.35"), Decimal("0.55")) == Decimal(1)
    assert conditional_floor_boost(Decimal("0.45"), Decimal("1.3"), Decimal("0.35"), Decimal("0.55")) == Decimal("1.15")
    assert conditional_floor_boost(Decimal("0.40"), Decimal("1.5"), Decimal("Inf"), Decimal("Inf")) == Decimal("1.5")
    rising = conditional_floor_boost(
        Decimal("0.37"), Decimal("1.5"), Decimal("0.35"), Decimal("0.55"),
        hysteresis=Decimal("0.03"), direction=1,
    )
    plain = conditional_floor_boost(
        Decimal("0.37"), Decimal("1.5"), Decimal("0.35"), Decimal("0.55"),
        hysteresis=Decimal("0.03"), direction=0,
    )
    assert rising == Decimal("1.5")
    assert plain < Decimal("1.5")
    pinned = step_boost(
        BoostState(Decimal("1.3"), 0, None),
        Decimal("0.90"),
        n_hosts=2,
        mode="conditional",
        b_max=Decimal("1.3"),
        u_low=Decimal("0.35"),
        u_high=Decimal("0.55"),
        hysteresis=Decimal("0.03"),
        smooth=Decimal("0.25"),
        min_hosts=3,
        thin_plateau=True,
    )
    assert pinned.applied == Decimal("1.3")
    static = step_boost(
        BoostState(Decimal("1.1"), 1, Decimal("0.2")),
        Decimal("0.9"),
        n_hosts=10,
        mode="static",
        b_max=Decimal("1.5"),
        u_low=Decimal("0.35"),
        u_high=Decimal("0.55"),
        hysteresis=Decimal("0.03"),
        smooth=Decimal("0.25"),
        min_hosts=3,
        thin_plateau=True,
    )
    assert static.applied == Decimal("1.5")


def test_tip_cap_never_exceeds_ten_percent():
    assert capped_tip_cents(500, 100, Decimal("0.10")) == 10
    assert capped_tip_cents(5, 105, Decimal("0.10")) == 5
    # 10% of 105 cents is 10.5; the allowed tip is floored to 10.
    assert capped_tip_cents(50, 105, Decimal("0.10")) == 10
    assert capped_tip_cents(50, 100, Decimal(0)) == 0


def test_notify_does_not_change_clearing_and_stop_drops_the_ladder():
    order = Order(
        "a", "org",
        (
            Rung("H", 150, 150, notify_cents=40, confirm_cents=80, auto_fallback_cents=100, stop_cents=None),
            Rung("L", 60, 60),
        ),
        hours=Decimal(1),
        lottery=1,
    )
    state = PainState(fallen_back={})
    seen, state = apply_pain(order, {"H": 50, "L": 20}, state, hysteresis=Decimal("0.05"))
    assert seen is not None
    assert seen.rungs[0].max_cents == 150
    assert state.notify_count == 1
    stopped, _state = apply_pain(
        Order("a", "org", (Rung("H", 150, 150, stop_cents=100), Rung("L", 60, 60)), hours=Decimal(1)),
        {"H": 100},
        PainState(fallen_back={}),
        hysteresis=Decimal("0.05"),
    )
    assert stopped is None


def test_illustrative_realtime_round_pays_base_plus_own_tip_only_when_scarce():
    orders = [
        _order("A", "A", [_rung("H", 150)], lottery=2),
        _order("B", "B", [_rung("H", 120), _rung("L", 60)], lottery=3),
        _order("C", "C", [_rung("H", 110), _rung("L", 50)], tip=5, lottery=11),
        _order("D", "D", [_rung("H", 90), _rung("L", 50)], lottery=4),
        _order("E", "E", [_rung("H", 130)], tip=10, lottery=10),
        _order("F", "F", [_rung("H", 105), _rung("L", 45)], lottery=1),
        _order("G", "G", [_rung("L", 70)], lottery=5),
        _order("J", "J", [_rung("L", 42)], lottery=6),
    ]
    pools = _pools(H=(4, 100, 1), L=(6, 40, 1))
    result = clear_realtime(orders, pools, share_cap=Decimal("0.25"), tip_cap=Decimal("0.10"))
    assert result.hit_iteration_cap is False
    assert result.passes <= 50
    assert result.pools["H"].demand == 5
    assert result.pools["L"].demand == 4
    assert result.pools["H"].scarce is True
    assert result.pools["L"].scarce is False
    pays = {fill.order_id: (fill.pool_id, fill.pay_cents) for fill in result.fills}
    assert pays["E"] == ("H", 110)
    assert pays["C"] == ("H", 105)
    assert pays["A"] == ("H", 100)
    assert pays["F"] == ("H", 100)
    for oid in ("B", "D", "G", "J"):
        assert pays[oid] == ("L", 40)
    for fill in result.fills:
        assert fill.pay_cents >= pools[fill.pool_id].reserve_cents
        order = next(item for item in orders if item.order_id == fill.order_id)
        assert fill.pay_cents <= max(rung.max_cents for rung in order.rungs)


def test_realtime_ignores_arrival_order_and_share_cap_binds():
    orders = [
        _order("A", "A", [_rung("H", 150)], lottery=2),
        _order("B", "B", [_rung("H", 120), _rung("L", 60)], lottery=3),
        _order("C", "C", [_rung("H", 110), _rung("L", 50)], tip=5, lottery=11),
        _order("E", "E", [_rung("H", 130)], tip=10, lottery=10),
        _order("F", "F", [_rung("H", 105), _rung("L", 45)], lottery=1),
    ]
    pools = _pools(H=(2, 100, 1), L=(3, 40, 1))
    forward = clear_realtime(orders, pools, share_cap=None, tip_cap=Decimal("0.10"))
    backward = clear_realtime(list(reversed(orders)), pools, share_cap=None, tip_cap=Decimal("0.10"))
    assert [(f.order_id, f.pool_id, f.pay_cents) for f in forward.fills] == [
        (f.order_id, f.pool_id, f.pay_cents) for f in backward.fills
    ]
    assert {fill.order_id for fill in forward.fills if fill.pool_id == "H"} == {"E", "C"}

    whale = [
        _order("A", "whale", [_rung("H", 500)], tip=100, lottery=1, hours="8"),
        _order("B", "b", [_rung("H", 500)], lottery=2, hours="2"),
        _order("C", "c", [_rung("H", 500)], lottery=3, hours="2"),
        _order("D", "d", [_rung("H", 500)], lottery=4, hours="2"),
        _order("E", "e", [_rung("H", 500)], lottery=5, hours="2"),
    ]
    capped = clear_realtime(whale, _pools(H=(8, 100, 50)), share_cap=Decimal("0.25"), tip_cap=None)
    shares = org_hours(capped, "H")
    assert shares["whale"] == Decimal(2)
    assert sum(shares.values()) == Decimal(8)
    assert capped.pools["H"].scarce is True


def test_never_clears_below_reserve_or_above_max():
    orders = [
        _order("low", "low", [_rung("H", 50)], lottery=1),
        _order("ok", "ok", [_rung("H", 300)], tip=500, lottery=2),
        _order("other", "other", [_rung("H", 300)], lottery=3),
    ]
    result = clear_realtime(orders, _pools(H=(1, 100, 100)), share_cap=None, tip_cap=Decimal("0.10"))
    assert [fill.order_id for fill in result.fills] == ["ok"]
    assert result.fills[0].pay_cents == 110
    assert result.pools["H"].scarce is True


def test_day_ahead_uniform_price_upgrade_and_share_cap():
    orders = [
        _order("A", "A", [_rung("H", 500), _rung("L", 500)], lottery=1),
        _order("B", "B", [_rung("H", 600)], lottery=2),
        _order("C", "C", [_rung("H", 400)], lottery=3),
    ]
    pools = _pools(H=(1, 100, 100), L=(5, 100, 100))
    result = clear_day_ahead(orders, pools, share_cap=None)
    won = {fill.order_id: fill.pool_id for fill in result.fills}
    assert won["B"] == "H"
    assert won["A"] == "L"
    assert "C" not in won
    assert result.final_price_cents["H"] == 100
    assert all(fill.pay_cents >= 100 for fill in result.fills)
    upgraded, gains = commit_upgrade_pass(orders, result, pools, None)
    assert gains == 0
    assert {fill.order_id: fill.pool_id for fill in upgraded.fills}["B"] == "H"

    climb = [
        _order("Big", "Big", [_rung("X", 900)], lottery=1),
        _order("S", "S", [_rung("X", 500), _rung("H", 500)], lottery=2),
        _order("U", "U", [_rung("H", 400), _rung("L", 400)], lottery=3),
        _order("Other", "Other", [_rung("L", 200)], lottery=4),
    ]
    pools2 = _pools(X=(1, 100, 100), H=(1, 100, 100), L=(1, 100, 100))
    downward = clear_day_ahead(climb, pools2, share_cap=None)
    assert {fill.order_id: fill.pool_id for fill in downward.fills}["U"] == "L"
    upgraded, gains = commit_upgrade_pass(climb, downward, pools2, None)
    assert gains >= 1
    assert {fill.order_id: fill.pool_id for fill in upgraded.fills}["U"] == "H"
    assert all(fill.pay_cents >= 100 for fill in upgraded.fills)

    capped_orders = [
        _order("A", "A", [_rung("H", 1000)], lottery=1, hours="4"),
        _order("B", "B", [_rung("H", 300)], lottery=2, hours="1"),
    ]
    capped = clear_day_ahead(capped_orders, _pools(H=(4, 100, 100)), share_cap=Decimal("0.25"))
    pays = {fill.order_id: fill.pay_cents for fill in capped.fills}
    assert pays["B"] <= 300
    assert all(pay >= 100 for pay in pays.values())


def test_iteration_cap_is_deterministic():
    orders = []
    pools = {}
    rungs = []
    for index in range(55):
        pid = f"p{index:02d}"
        pools[pid] = PoolView(pid, Decimal(1), 100, 10)
        rungs.append(_rung(pid, 200))
        orders.append(_order(f"block{index:02d}", f"block{index:02d}", [_rung(pid, 200)], tip=100, lottery=index))
    traveler = _order("traveler", "traveler", rungs, lottery=10_000)
    first = clear_realtime(orders + [traveler], pools, share_cap=None, tip_cap=None, max_passes=50)
    second = clear_realtime(list(reversed(orders + [traveler])), pools, share_cap=None, tip_cap=None, max_passes=50)
    assert first.hit_iteration_cap is True
    assert first.passes == 50
    assert [(f.order_id, f.pool_id, str(f.hours)) for f in first.fills] == [
        (f.order_id, f.pool_id, str(f.hours)) for f in second.fills
    ]


def test_token_quote_covers_the_reference_hour():
    floor = _floor("1.275", "0.1419", "1.3", "35625")
    reserve = reserve_cents(floor, Decimal("0.12"), Decimal(1))
    ref = reference_cents(reserve, 0)
    price = price_per_million_cents(ref, Decimal(12345), Decimal("0.5"))
    revenue = implied_hourly_cents(price, Decimal(12345), Decimal("0.5"))
    assert revenue >= ref
    with pytest.raises(ValueError):
        price_per_million_cents(ref, Decimal(0), Decimal("0.5"))
