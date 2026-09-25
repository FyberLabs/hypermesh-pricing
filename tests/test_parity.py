"""pricing_core matches committed market-sim fixtures within one cent.

The fixtures are simulated outputs. CI does not fetch the private
market-sim reference. Regenerate them locally with
scripts/regenerate_fixtures.py when you have a checkout.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from pricing_core.boost import conditional_floor_boost
from pricing_core.clearing import PoolView, clear_day_ahead, clear_realtime, commit_upgrade_pass
from pricing_core.controller import corridor_cap_cents, step_base_cents
from pricing_core.engine import price_day_ahead, price_realtime
from pricing_core.floor import floor_dollars, reserve_dollars
from pricing_core.orders import Order, Rung

FIXTURES = Path(__file__).resolve().parent / "fixtures"
U_MIN = Decimal("0.30")
U_MAX = Decimal("0.70")


def test_floor_matches_the_fixture_within_a_cent():
    for case in _load("floor.json")["cases"]:
        spec = case["input"]
        floor, _u = floor_dollars(
            Decimal(spec["loaded_kw"]),
            Decimal(spec["tariff_per_kwh"]),
            Decimal(spec["overhead"]),
            Decimal(spec["capex"]),
            Decimal(spec["residual"]),
            Decimal(spec["u"]),
            Decimal(spec["host_margin"]),
            years=Decimal(3),
            hours_per_year=Decimal(8760),
            u_min=U_MIN,
            u_max=U_MAX,
        )
        reserve = reserve_dollars(floor, Decimal(spec["take"]), Decimal(spec["boost"]))
        assert abs(float(floor) - float(case["expected"]["floor"])) < 1e-9
        assert abs(float(reserve) - float(case["expected"]["reserve"])) < 1e-9


def test_boost_schedule_matches_the_fixture():
    for case in _load("boost.json")["cases"]:
        spec = case["input"]
        got = conditional_floor_boost(
            Decimal(spec["utilization"]),
            Decimal(spec["b_max"]),
            Decimal(spec["u_low"]),
            Decimal(spec["u_high"]),
            hysteresis=Decimal(spec["hysteresis"]),
            direction=spec["direction"],
        )
        assert abs(float(got) - float(case["expected"])) < 1e-12


def test_controller_matches_the_fixture_within_a_cent():
    body = _load("controller.json")["cases"]
    for case in body["steps"]:
        spec = case["input"]
        got = step_base_cents(
            _cents(spec["base"]),
            Decimal(spec["utilization"]),
            Decimal(spec["target"]),
            Decimal(spec["delta"]),
            _cents(spec["reserve"]),
            _cents(spec["cap"]),
        )
        _near(got, case["expected"])
        assert got >= _cents(spec["reserve"])
    corridor = body["corridor"]
    prices = [Decimal(price) for price in corridor["input"]["day_ahead_prices"]]
    ordered = sorted(prices)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 == 1 else (ordered[mid - 1] + ordered[mid]) / 2
    got_cap = corridor_cap_cents(
        _cents(corridor["input"]["reserve"]),
        _cents(median),
        Decimal(corridor["input"]["kappa"]),
        Decimal(corridor["input"]["lambda"]),
    )
    _near(got_cap, corridor["expected"])


def test_illustrative_realtime_matches_the_fixture():
    body = _load("realtime.json")["cases"]
    result = clear_realtime(
        _orders(body["orders"]),
        _pools(body["pools"]),
        share_cap=_optional_decimal(body["share_cap"]),
        tip_cap=_optional_decimal(body["tip_cap"]),
    )
    _assert_clearing(result, body["expected"])


def test_day_ahead_and_upgrade_match_the_fixture():
    body = _load("day_ahead.json")["cases"]
    orders = _orders(body["orders"], kind="day_ahead")
    pools = _pools(body["pools"])
    share = _optional_decimal(body["share_cap"])
    result = clear_day_ahead(orders, pools, share_cap=share)
    _assert_clearing(result, body["expected_down"])
    upgraded, gains = commit_upgrade_pass(orders, result, pools, share)
    assert gains == body["expected_upgrade"]["gains"]
    _assert_fills(upgraded.fills, body["expected_upgrade"]["fills"])


def test_engine_realtime_matches_the_fixture_and_is_deterministic():
    body = _load("engine_realtime.json")["cases"]
    request = body["request"]
    priced = price_realtime(request)
    pays = {fill["order_id"]: fill for fill in priced["fills"]}
    for expected in body["expected_fills"]:
        got = pays[expected["order_id"]]
        assert got["pool_id"] == expected["pool_id"]
        _near(got["pay_cents"], expected["pay"])
    reversed_request = {
        **request,
        "pools": list(reversed(request["pools"])),
        "orders": list(reversed(request["orders"])),
    }
    again = price_realtime(reversed_request)
    assert again["fills"] == priced["fills"]
    assert again["pools"] == priced["pools"]


def test_day_ahead_share_is_clamped_at_seventy_five_percent():
    result = price_day_ahead(
        {
            "round_id": "da-cap",
            "pools": [
                {
                    "pool_id": "H",
                    "class_id": "class",
                    "region": "lab",
                    "supply_hours": "90",
                    "n_hosts": 10,
                    "reserve_cents": 100,
                    "prev_base_cents": 100,
                    "trailing_util_24h": "0.4",
                    "offered_hours": "100",
                    "boost": {"applied": "1.3", "direction": 0, "prev_trail": None},
                }
            ],
            "orders": [
                {
                    "order_id": "A",
                    "org_id": "A",
                    "lottery": 1,
                    "tip_cents": 0,
                    "hours": "80",
                    "rungs": [{"pool_id": "H", "max_cents": 500}],
                }
            ],
        }
    )
    pool = result["pools"][0]
    assert pool["supply_clamped"] is True
    assert pool["supply_hours"] == "75"
    assert pool["base_cents"] >= pool["reserve_cents"]
    assert pool["next_base_cents"] <= pool["cap_cents"]
    assert pool["next_base_cents"] >= pool["reserve_cents"]


def test_fixtures_are_labeled_simulated():
    for path in sorted(FIXTURES.glob("*.json")):
        body = json.loads(path.read_text(encoding="utf-8"))
        assert body["simulated"] is True
        assert "Simulated" in body["note"]
        assert "Not measured" in body["note"]


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _orders(specs: list[dict], *, kind: str = "realtime") -> list[Order]:
    return [
        Order(
            order_id=spec["order_id"],
            org_id=spec["org_id"],
            rungs=tuple(
                Rung(
                    pool_id=rung["pool_id"],
                    max_cents=_cents(rung["max"]),
                    willingness_cents=_cents(rung["max"]),
                )
                for rung in spec["rungs"]
            ),
            hours=Decimal(spec["hours"]),
            tip_cents=_cents(spec["tip"]),
            lottery=spec["lottery"],
            kind=kind,
        )
        for spec in specs
    ]


def _pools(specs: list[dict]) -> dict[str, PoolView]:
    return {
        spec["pool_id"]: PoolView(
            spec["pool_id"],
            supply=Decimal(spec["supply"]),
            base_cents=_cents(spec["base"]),
            reserve_cents=_cents(spec["reserve"]),
        )
        for spec in specs
    }


def _assert_clearing(result, expected: dict) -> None:
    assert result.passes == expected["passes"]
    for pool_id, scarce in expected["scarce"].items():
        assert result.pools[pool_id].scarce is scarce
    _assert_fills(result.fills, expected["fills"])


def _assert_fills(core_fills, expected: list[dict]) -> None:
    core = {(fill.order_id, fill.pool_id): fill for fill in core_fills}
    golden = {(fill["order_id"], fill["pool_id"]): fill for fill in expected}
    assert set(core) == set(golden)
    for key, want in golden.items():
        got = core[key]
        _near(got.pay_cents, want["pay"])
        assert abs(float(got.hours) - float(want["hours"])) <= 1e-9
        assert got.rung_index == want["rung_index"]


def _optional_decimal(value):
    if value is None:
        return None
    return Decimal(value)


def _cents(dollars: str) -> int:
    return int((Decimal(dollars) * 100).to_integral_value())


def _near(cents: int, dollars: str) -> None:
    assert abs(cents - float(dollars) * 100) <= 1.0 + 1e-6
