"""Customer-facing ruleset copy is generated from the parameters."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from pricing_core.engine import price_realtime
from pricing_core.ruleset import RulesetError, load_ruleset, validate_ruleset
from pricing_core.transparency import choose_active, public_lines

PUBLISHED_PHRASES = (
    "Prices move at most 2.5% per 15 minutes",
    "Spike cap: 3x the floor-based reserve",
    "Priority tip capped at 10% of the live price",
    "New or quiet pools get up to a 1.3x floor boost that fades as the pool reaches 55% busy",
    "No org may take more than 25% of a scarce pool",
)


def test_published_lines_match_the_generator_and_the_memo_phrases():
    rules = load_ruleset("2026-09-25.1")
    generated = public_lines(rules.raw)
    assert rules.raw["public"]["lines"] == generated
    for phrase in PUBLISHED_PHRASES:
        assert phrase in generated
    assert rules.raw["effective_from"] == "2026-09-25T00:00:00Z"
    assert rules.raw["changelog"]["previous_version"] is None
    assert "previous version" in rules.raw["changelog"]["summary"]
    blob = " ".join(generated)
    assert "5547" not in blob
    assert "seed" not in blob.lower()


def test_each_customer_visible_knob_changes_the_lines_and_internals_do_not():
    raw = load_ruleset("2026-09-25.1").raw
    original = public_lines(raw)
    visible = {
        ("floor", "depreciation_years"): 4,
        ("floor", "u_min"): "0.31",
        ("floor", "u_max"): "0.71",
        ("floor", "u_default"): "0.51",
        ("boost", "mode"): "static",
        ("boost", "b_max"): "1.4",
        ("boost", "u_low"): "0.36",
        ("boost", "u_high"): "0.56",
        ("boost", "window_hours"): 12,
        ("boost", "min_hosts"): 4,
        ("controller", "delta"): "0.050",
        ("controller", "target_util"): "0.71",
        ("controller", "kappa"): "4",
        ("controller", "lambda"): "1.6",
        ("controller", "round_minutes"): 30,
        ("market", "tip_cap"): "0.11",
        ("market", "share_cap"): "0.26",
        ("market", "day_ahead_share_max"): "0.76",
        ("market", "day_ahead_upgrade"): False,
    }
    for (section, key), value in visible.items():
        mutated = copy.deepcopy(raw)
        mutated[section][key] = value
        assert public_lines(mutated) != original, f"{section}.{key} did not change the public lines"
    hidden = {
        ("floor", "hours_per_year"): 9000,
        ("floor", "residual_default"): "1",
        ("boost", "hysteresis"): "0.05",
        ("boost", "smooth"): "0.50",
        ("boost", "thin_plateau"): False,
        ("market", "max_passes"): 10,
        ("market", "pain_hysteresis"): "0.10",
        ("market", "smoothing_alpha"): "0.50",
    }
    for (section, key), value in hidden.items():
        mutated = copy.deepcopy(raw)
        mutated[section][key] = value
        assert public_lines(mutated) == original


def test_a_stale_public_block_is_rejected():
    raw = json.loads(json.dumps(load_ruleset("2026-09-25.1").raw))
    raw["controller"]["delta"] = "0.05"
    with pytest.raises(RulesetError, match="public lines"):
        validate_ruleset(raw)
    raw["public"]["lines"] = public_lines(raw)
    validate_ruleset(raw)
    raw["changelog"]["previous_version"] = raw["version"]
    with pytest.raises(RulesetError, match="previous_version"):
        validate_ruleset(raw)


def test_a_future_ruleset_is_not_the_active_one():
    early = datetime(2026, 9, 25, tzinfo=timezone.utc)
    later = datetime(2026, 10, 1, tzinfo=timezone.utc)
    entries = [("2026-09-25.1", early), ("2026-10-01.1", later)]
    assert choose_active(entries, datetime(2026, 9, 25, 0, 15, tzinfo=timezone.utc)) == "2026-09-25.1"
    assert choose_active(entries, later) == "2026-10-01.1"
    with pytest.raises(ValueError, match="no ruleset is effective"):
        choose_active(entries, datetime(2026, 9, 24, tzinfo=timezone.utc))


def test_pool_flags_explain_the_live_price():
    quiet = price_realtime(_pool_round(trailing="0.20", prev=100, reserve=100, hours="1"))
    pool = quiet["pools"][0]
    assert pool["boost_active"] is True
    assert pool["boost_multiple"] == "1.3"
    assert pool["at_floor"] is True
    assert pool["at_cap"] is False
    assert pool["scarce"] is False
    assert pool["org_cap_applied"] is False
    assert pool["degraded"] is False
    assert pool["ruleset_version"] == quiet["ruleset_version"] == "2026-09-25.1"

    faded = price_realtime(
        _pool_round(
            trailing="0.90",
            prev=100,
            reserve=100,
            hours="1",
            boost={"applied": "1", "direction": -1, "prev_trail": "0.90"},
        )
    )
    assert faded["pools"][0]["boost_active"] is False
    assert faded["pools"][0]["boost_multiple"] == "1"

    capped = price_realtime(_pool_round(trailing="0.90", prev=300, reserve=100, hours="1"))
    assert capped["pools"][0]["at_cap"] is True
    assert capped["pools"][0]["at_floor"] is False
    assert capped["pools"][0]["base_cents"] == 300

    scarce = _pool_round(trailing="0.20", prev=100, reserve=100, hours="8", supply="8")
    scarce["orders"] = [
        _order("whale", "whale", 1, "8"),
        _order("b", "b", 2, "2"),
        _order("c", "c", 3, "2"),
        _order("d", "d", 4, "2"),
        _order("e", "e", 5, "2"),
    ]
    cleared = price_realtime(scarce)
    assert cleared["pools"][0]["scarce"] is True
    assert cleared["pools"][0]["org_cap_applied"] is True
    whale_hours = sum(
        (Decimal(fill["hours"]) for fill in cleared["fills"] if fill["org_id"] == "whale"),
        Decimal(0),
    )
    assert whale_hours == Decimal(2)

    local = price_realtime({**_pool_round(trailing="0.20", prev=100, reserve=100, hours="1"), "degraded": True})
    assert local["pools"][0]["degraded"] is True


def _pool_round(*, trailing: str, prev: int, reserve: int, hours: str, supply: str = "4", boost: dict | None = None) -> dict:
    return {
        "round_id": "r-flags",
        "round_start": "2026-09-25T00:15:00Z",
        "ruleset_version": "2026-09-25.1",
        "pools": [
            {
                "pool_id": "H",
                "class_id": "h100",
                "region": "us-east",
                "supply_hours": supply,
                "n_hosts": 8,
                "reserve_cents": reserve,
                "prev_base_cents": prev,
                "trailing_util_24h": trailing,
                "boost": boost or {"applied": "1.3", "direction": 0, "prev_trail": None},
            }
        ],
        "orders": [_order("A", "A", 1, hours)],
    }


def _order(order_id: str, org_id: str, lottery: int, hours: str) -> dict:
    return {
        "order_id": order_id,
        "org_id": org_id,
        "lottery": lottery,
        "tip_cents": 0,
        "hours": hours,
        "rungs": [{"pool_id": "H", "max_cents": 500}],
    }
