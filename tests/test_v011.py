"""v0.1.1 integration contract: package data, degraded rounds, tiers, fees."""

from __future__ import annotations

import hashlib
import hmac
import os
import subprocess
import sys
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pricing_core import ENGINE_VERSION, canonical_pool_id, degraded_round, derive_lottery
from pricing_core.engine import price_day_ahead, price_floor, price_realtime, price_tokens
from pricing_core.floor import fixed_fee_per_hour, reserve_cents
from pricing_core.hashing import request_hash
from pricing_core.lottery import lottery_commitment
from pricing_service.app import TOKEN_ENV, create_app

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token"


def test_version_is_0_1_1():
    assert ENGINE_VERSION == "0.1.1"


def test_wheel_install_loads_the_active_ruleset(tmp_path: Path):
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    subprocess.check_call(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(wheel_dir), str(ROOT)],
        cwd=tmp_path,
    )
    wheel = next(wheel_dir.glob("*.whl"))
    names = zipfile.ZipFile(wheel).namelist()
    assert "pricing_core/rulesets/2026-09-25.1.json" in names
    assert "pricing_core/rulesets/2026-09-25.2.json" in names
    venv = tmp_path / "venv"
    subprocess.check_call([sys.executable, "-m", "venv", str(venv)])
    pip = venv / "bin" / "pip"
    python = venv / "bin" / "python"
    subprocess.check_call([str(pip), "install", "--no-deps", str(wheel)])
    script = (
        "import os\n"
        "os.environ.pop('PRICING_RULESET_DIR', None)\n"
        "os.chdir('/tmp')\n"
        "from pricing_core.ruleset import load_ruleset\n"
        "rules = load_ruleset()\n"
        "assert rules.version == '2026-09-25.2', rules.version\n"
        "assert rules.market['lock_hours_max'] == 24\n"
    )
    env = os.environ.copy()
    env.pop("PRICING_RULESET_DIR", None)
    subprocess.check_call([str(python), "-c", script], cwd="/tmp", env=env)


def test_env_override_still_selects_a_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PRICING_RULESET_DIR", str(tmp_path))
    from pricing_core.ruleset import RulesetError, ruleset_dir

    assert ruleset_dir() == tmp_path
    monkeypatch.setenv("PRICING_RULESET_DIR", str(tmp_path / "missing"))
    with pytest.raises(RulesetError):
        ruleset_dir()


def test_degraded_round_holds_base_and_freezes_memory_and_still_clears():
    payload = _round(prev=180, ema=150, trailing="0.10")
    payload["orders"] = [
        {
            "order_id": "spot",
            "org_id": "spot",
            "lottery": 1,
            "tip_cents": 50,
            "hours": "1",
            "rungs": [{"pool_id": "H", "max_cents": 500}],
        }
    ]
    held = degraded_round(payload)
    live = price_realtime(payload)
    pool = held["pools"][0]
    assert pool["degraded"] is True
    assert pool["base_cents"] == 180
    assert pool["next_base_cents"] == 180
    assert pool["next_ema_cents"] == 150
    assert pool["boost"] == payload["pools"][0]["boost"] | {"prev_trail": None}
    assert live["pools"][0]["next_base_cents"] != pool["next_base_cents"] or live["pools"][0]["boost"] != pool["boost"]
    assert held["fills"][0]["pay_cents"] == 180
    assert held["fills"][0]["pay_cents"] >= pool["reserve_cents"]
    with pytest.raises(Exception, match="day-ahead is refused"):
        price_day_ahead({**payload, "degraded": True, "pools": [{**payload["pools"][0], "offered_hours": "4"}]})


def test_fill_total_is_box_hours_plus_tip_total():
    body = price_realtime(
        {
            **_round(prev=100, ema=100, trailing="0.20"),
            "pools": [{**_round(prev=100, ema=100, trailing="0.20")["pools"][0], "supply_hours": "4"}],
            "orders": [
                {
                    "order_id": "tipper",
                    "org_id": "tipper",
                    "lottery": 1,
                    "tip_cents": 10,
                    "hours": "1",
                    "rungs": [{"pool_id": "H", "max_cents": 500}],
                },
                {
                    "order_id": "other",
                    "org_id": "other",
                    "lottery": 2,
                    "tip_cents": 0,
                    "hours": "4",
                    "rungs": [{"pool_id": "H", "max_cents": 500}],
                },
            ],
        }
    )
    fill = next(item for item in body["fills"] if item["order_id"] == "tipper")
    assert fill["pay_cents"] == 100
    assert fill["tip_cents"] == 10
    assert fill["hours"] == "1"
    assert fill["tip_total_cents"] == 10
    assert fill["total_cents"] == 100 * 1 + 10
    assert fill["price_lock_hours"] == "1"


def test_price_lock_caps_a_long_fill_at_the_ruleset_param():
    body = price_realtime(
        {
            **_round(prev=100, ema=100, trailing="0.80"),
            "pools": [{**_round(prev=100, ema=100, trailing="0.80")["pools"][0], "supply_hours": "40"}],
            "orders": [
                {
                    "order_id": "lease",
                    "org_id": "lease",
                    "lottery": 1,
                    "tip_cents": 0,
                    "hours": "30",
                    "rungs": [{"pool_id": "H", "max_cents": 500}],
                }
            ],
        }
    )
    assert body["price_lock_hours_max"] == 24
    assert body["fills"][0]["price_lock_hours"] == "24"
    assert body["ruleset_version"] == "2026-09-25.2"


def test_supply_tiers_sell_only_boxes_at_or_below_the_price():
    cheap = price_realtime(
        {
            "round_id": "tiers",
            "round_start": "2026-09-25T00:15:00Z",
            "ruleset_version": "2026-09-25.2",
            "pools": [
                {
                    "pool_id": "H",
                    "class_id": "h100",
                    "region": "default",
                    "n_hosts": 8,
                    "supply_tiers": [
                        {"reserve_cents": 100, "hours": "2"},
                        {"reserve_cents": 300, "hours": "3"},
                    ],
                    "prev_base_cents": 100,
                    "trailing_util_24h": "0.80",
                    "boost": {"applied": "1", "direction": 0},
                }
            ],
            "orders": [
                {
                    "order_id": "want",
                    "org_id": "want",
                    "lottery": 1,
                    "hours": "4",
                    "rungs": [{"pool_id": "H", "max_cents": 500}],
                }
            ],
        }
    )
    pool = cheap["pools"][0]
    assert pool["reserve_cents"] == 100
    assert pool["reserve_source"] == "tiers"
    assert pool["base_cents"] == 100
    assert pool["supply_hours"] == "2"
    assert cheap["fills"][0]["pay_cents"] == 100
    assert Decimal(cheap["fills"][0]["hours"]) == Decimal(2)

    rich = price_realtime(
        {
            "round_id": "tiers-rich",
            "round_start": "2026-09-25T00:15:00Z",
            "ruleset_version": "2026-09-25.2",
            "pools": [
                {
                    "pool_id": "H",
                    "class_id": "h100",
                    "region": "default",
                    "n_hosts": 8,
                    "supply_tiers": [
                        {"reserve_cents": 100, "hours": "2"},
                        {"reserve_cents": 300, "hours": "3"},
                    ],
                    "prev_base_cents": 300,
                    "trailing_util_24h": "0.80",
                    "boost": {"applied": "1", "direction": 0},
                }
            ],
            "orders": [
                {
                    "order_id": "want",
                    "org_id": "want",
                    "lottery": 1,
                    "hours": "4",
                    "rungs": [{"pool_id": "H", "max_cents": 500}],
                }
            ],
        }
    )
    assert rich["pools"][0]["base_cents"] == 300
    assert rich["pools"][0]["supply_hours"] == "5"
    assert rich["fills"][0]["pay_cents"] == 300
    assert Decimal(rich["fills"][0]["hours"]) == Decimal(4)


def test_single_reserve_form_still_clears():
    body = price_realtime(_round(prev=100, ema=100, trailing="0.80"))
    assert body["pools"][0]["reserve_source"] == "request"
    assert body["fills"]


def test_host_net_at_the_reserve_covers_floor_after_take_and_processor_fees():
    floor = Decimal("1.25")
    take = Decimal("0.08")
    boost = Decimal("1.3")
    pct = Decimal("0.029")
    fixed_cents = 30
    hours = Decimal("2")
    per_hour = fixed_fee_per_hour(fixed_cents, hours)
    cents = reserve_cents(
        floor,
        take,
        boost,
        processor_pct=pct,
        processor_fixed_per_hour=per_hour,
    )
    reserve = Decimal(cents) / Decimal(100)
    host_net = reserve * (Decimal(1) - take - pct) - per_hour
    assert host_net >= floor * boost
    assert host_net >= floor

    quoted = price_floor(
        {
            "loaded_kw": "0.070",
            "tariff_per_kwh": "0.1834",
            "overhead": "1.05",
            "capex": "1999",
            "u": "0.50",
            "take": "0.08",
            "boost": "1.3",
            "processor_pct": "0.029",
            "processor_fixed_cents": 30,
            "expected_hours_per_txn": "2",
        }
    )
    reserve_q = Decimal(quoted["reserve_cents"]) / Decimal(100)
    floor_q = Decimal(quoted["floor_usd"])
    host_q = reserve_q * (Decimal(1) - Decimal("0.08") - Decimal("0.029")) - (
        Decimal(30) / Decimal(100) / Decimal(2)
    )
    assert host_q >= floor_q * Decimal("1.3")


def test_token_prices_use_the_caller_take_and_stay_above_the_reserve():
    body = price_tokens(
        {
            "pool_id": "h100@default",
            "class_id": "h100",
            "model_id": "example",
            "base_cents": 200,
            "reserve_cents": 150,
            "take": "0.10",
            "input_tokens_per_second": "100",
            "output_tokens_per_second": "50",
            "u_batch": "1",
            "ruleset_version": "2026-09-25.2",
        }
    )
    assert body["reference_cents"] == 200
    assert body["billed_hour_cents"] >= 200
    assert body["loaded_hour_min_cents"] == 150
    assert body["input_per_1k_cents"] > 0
    assert body["output_per_1k_cents"] > body["input_per_1k_cents"]
    assert body["take"] == "0.1"
    assert "request_hash" in body


def test_lottery_is_hmac_sha256_of_the_round_seed():
    seed = b"round-seed"
    digest = hmac.new(seed, b"order-a", hashlib.sha256).digest()
    assert derive_lottery(seed, "order-a") == int.from_bytes(digest[:8], "big")
    assert derive_lottery(seed, "order-a") != derive_lottery(seed, "order-b")
    assert lottery_commitment(seed) == hashlib.sha256(seed).hexdigest()
    assert canonical_pool_id("h100") == "h100@default"
    assert canonical_pool_id("h100", "us-east") == "h100@us-east"


def test_request_hash_ignores_key_order_and_the_idempotency_key():
    left = {"b": 1, "a": [2, 3], "idempotency_key": "retry-1"}
    right = {"a": [2, 3], "b": 1}
    assert request_hash(left) == request_hash(right)
    assert request_hash(left) != request_hash({**right, "a": [3, 2]})


def test_http_echoes_idempotency_key_and_prices_tokens(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    client = TestClient(create_app())
    headers = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "round-9"}
    first = client.post("/v1/rounds/realtime", json=_round(prev=100, ema=100, trailing="0.80"), headers=headers)
    assert first.status_code == 200, first.text
    assert first.headers["idempotency-key"] == "round-9"
    assert first.json()["idempotency_key"] == "round-9"
    second = client.post(
        "/v1/rounds/realtime",
        json=_round(prev=100, ema=100, trailing="0.80"),
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert second.json()["request_hash"] == first.json()["request_hash"]
    assert second.json()["idempotency_key"] is None
    refused = client.post(
        "/v1/rounds/day-ahead",
        json={**_round(prev=100, ema=100, trailing="0.80"), "degraded": True},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert refused.status_code == 422
    tokens = client.post(
        "/v1/token-prices",
        json={
            "class_id": "h100",
            "model_id": "example",
            "base_cents": 200,
            "reserve_cents": 150,
            "take": "0.10",
            "input_tokens_per_second": "100",
            "output_tokens_per_second": "50",
        },
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert tokens.status_code == 200, tokens.text
    assert tokens.json()["loaded_hour_min_cents"] == 150
    open_card = client.get("/v1/rulesets/active")
    assert open_card.status_code == 200
    protected = client.post("/v1/token-prices", json={})
    assert protected.status_code == 401


def _round(*, prev: int, ema: int, trailing: str) -> dict:
    return {
        "round_id": "r",
        "round_start": "2026-09-25T00:15:00Z",
        "ruleset_version": "2026-09-25.2",
        "pools": [
            {
                "pool_id": "H",
                "class_id": "h100",
                "region": "default",
                "supply_hours": "4",
                "n_hosts": 8,
                "reserve_cents": 100,
                "prev_base_cents": prev,
                "ema_cents": ema,
                "trailing_util_24h": trailing,
                "boost": {"applied": "1.3", "direction": 0, "prev_trail": None},
            }
        ],
        "orders": [
            {
                "order_id": "A",
                "org_id": "A",
                "lottery": 1,
                "tip_cents": 0,
                "hours": "1",
                "rungs": [{"pool_id": "H", "max_cents": 500}],
            }
        ],
    }
