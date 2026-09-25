"""HTTP contract: auth, health, floors, rounds, rulesets, OpenAPI."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pricing_service.app import TOKEN_ENV, create_app

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    return TestClient(create_app())


def test_core_imports_are_stdlib_only():
    allowed = {
        "pricing_core",
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "hashlib",
        "hmac",
        "json",
        "os",
        "pathlib",
        "__future__",
    }
    for path in (ROOT / "pricing_core").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in modules:
                assert name in allowed, f"{path.name} imports {name}"


def test_process_refuses_to_start_without_a_token(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    from pricing_service.__main__ import main, require_token

    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with pytest.raises(SystemExit) as missing:
        require_token()
    assert missing.value.code == 1
    assert capsys.readouterr().err.strip() == f"{TOKEN_ENV} is required"

    monkeypatch.setenv(TOKEN_ENV, "   ")
    with pytest.raises(SystemExit) as blank:
        require_token()
    assert blank.value.code == 1
    assert capsys.readouterr().err.strip() == f"{TOKEN_ENV} is required"

    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    require_token()

    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert err.strip() == f"{TOKEN_ENV} is required"
    assert TOKEN not in err


def test_healthz_is_open_and_v1_requires_a_bearer_token(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["engine_version"] == "0.1.1"
    missing = client.post("/v1/floors", json={})
    assert missing.status_code == 401
    wrong = client.post("/v1/floors", json={}, headers={"Authorization": "Bearer no"})
    assert wrong.status_code == 401
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    bare = TestClient(create_app())
    assert bare.get("/healthz").status_code == 200
    assert bare.get("/v1/rulesets").status_code == 200
    assert bare.get("/v1/rulesets/active").status_code == 200
    assert bare.post("/v1/floors", json={}).status_code == 503


def test_floors_require_take_and_ceil_the_jetson_row(client: TestClient):
    denied = client.post(
        "/v1/floors",
        json={"loaded_kw": "0.070", "tariff_per_kwh": "0.1834", "overhead": "1.05", "capex": "1999", "boost": "1"},
        headers=_auth(),
    )
    assert denied.status_code == 422
    ok = client.post(
        "/v1/floors",
        json={
            "loaded_kw": "0.070",
            "tariff_per_kwh": "0.1834",
            "overhead": "1.05",
            "capex": "1999",
            "u": "0.50",
            "take": "0.08",
            "boost": "1",
        },
        headers=_auth(),
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["ruleset_version"] == "2026-09-25.2"
    assert body["engine_version"] == "0.1.1"
    assert body["request_hash"]
    # Memo table prints $0.166 and $0.180. Ceiling cents stay within 1 cent
    # of those rounded figures and are never below the exact reserve.
    assert abs(body["floor_cents"] - 16.6) <= 1
    assert abs(body["reserve_cents"] - 18.0) <= 1
    assert body["reserve_cents"] >= body["floor_cents"]


def test_realtime_day_ahead_and_rulesets(client: TestClient):
    realtime = client.post("/v1/rounds/realtime", json=_round(day_ahead=False), headers=_auth())
    assert realtime.status_code == 200, realtime.text
    body = realtime.json()
    assert body["round_id"] == "r-1"
    assert body["ruleset_version"] == "2026-09-25.2"
    assert body["engine_version"] == "0.1.1"
    assert body["request_hash"]
    pool = body["pools"][0]
    assert pool["base_cents"] >= pool["reserve_cents"]
    assert pool["next_base_cents"] >= pool["reserve_cents"]
    assert pool["next_base_cents"] <= pool["cap_cents"]
    assert pool["ruleset_version"] == body["ruleset_version"]
    assert pool["degraded"] is False
    assert pool["at_floor"] is True
    assert pool["at_cap"] is False
    assert pool["scarce"] is False
    assert pool["org_cap_applied"] is False
    assert pool["boost_active"] is True
    assert pool["boost_multiple"] == "1.3"
    assert "lottery" not in pool
    assert {fill["order_id"] for fill in body["fills"]} == {"E", "C"}
    for fill in body["fills"]:
        assert fill["pay_cents"] >= pool["reserve_cents"]
        assert fill["pay_cents"] <= 500

    ahead = client.post("/v1/rounds/day-ahead", json=_round(day_ahead=True), headers=_auth())
    assert ahead.status_code == 200, ahead.text
    ahead_body = ahead.json()
    assert ahead_body["upgrade_applied"] is True
    assert ahead_body["pools"][0]["supply_hours"] == "3"
    assert ahead_body["pools"][0]["supply_clamped"] is True

    listed = client.get("/v1/rulesets")
    assert listed.status_code == 200
    versions = [item["version"] for item in listed.json()["rulesets"]]
    assert versions == ["2026-09-25.1", "2026-09-25.2"]
    assert "sha256" not in listed.json()["rulesets"][0]
    assert "source" not in listed.json()["rulesets"][0]
    assert listed.json()["default"] == "2026-09-25.2"

    missing = client.post(
        "/v1/rounds/realtime",
        json={**_round(day_ahead=False), "ruleset_version": "1999-01-01.0"},
        headers=_auth(),
    )
    assert missing.status_code == 404

    active = client.get("/v1/rulesets/active")
    assert active.status_code == 200, active.text
    card = active.json()
    named = client.get("/v1/rulesets/2026-09-25.2")
    assert named.status_code == 200, named.text
    assert named.json() == card
    previous = client.get("/v1/rulesets/2026-09-25.1")
    assert previous.status_code == 200, previous.text
    assert previous.json()["active"] is False
    assert previous.json()["changelog"]["previous_version"] is None
    assert card["version"] == "2026-09-25.2"
    assert card["active"] is True
    assert card["effective_from"] == "2026-09-25T00:00:00Z"
    assert card["changelog"]["previous_version"] == "2026-09-25.1"
    assert card["customer_visible"]["public_summary"] is True
    assert "sha256" not in card
    assert "source" not in card
    assert "Prices move at most 2.5% per 15 minutes" in card["public_summary"]
    assert "A real-time fill locks its price for up to 24 hours" in card["public_summary"]
    visible = {row["path"]: row for row in card["parameters"]}
    assert visible["controller.delta"]["customer_visible"] is True
    assert visible["controller.delta"]["value"] == "0.025"
    assert visible["market.lock_hours_max"]["value"] == 24
    assert "market.max_passes" not in visible
    assert "boost.hysteresis" not in visible
    dumped = json.dumps(card)
    assert "5547" not in dumped
    assert "seed" not in dumped.lower()
    assert "lottery" not in card
    assert "orders" not in card
    unknown = client.get("/v1/rulesets/1999-01-01.0")
    assert unknown.status_code == 404
    labeled = client.post(
        "/v1/rounds/realtime",
        json={**_round(day_ahead=False), "degraded": True},
        headers=_auth(),
    )
    assert labeled.status_code == 200, labeled.text
    assert labeled.json()["pools"][0]["degraded"] is True
    assert labeled.json()["pools"][0]["next_base_cents"] == labeled.json()["pools"][0]["base_cents"]


def test_openapi_document_matches_the_app(client: TestClient):
    generated = client.app.openapi()
    path = ROOT / "openapi" / "openapi.json"
    committed = json.loads(path.read_text(encoding="utf-8"))
    assert committed == generated
    for route in (
        "/healthz",
        "/v1/rulesets",
        "/v1/rulesets/active",
        "/v1/rulesets/{version}",
        "/v1/floors",
        "/v1/rounds/realtime",
        "/v1/rounds/day-ahead",
        "/v1/token-prices",
    ):
        assert route in committed["paths"]
    assert "bearer" in json.dumps(committed["components"].get("securitySchemes", {})).lower() or any(
        "HTTPBearer" in json.dumps(item) for item in committed["paths"]["/v1/floors"].values()
    )


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _round(*, day_ahead: bool) -> dict:
    pool = {
        "pool_id": "H",
        "class_id": "h100",
        "region": "us-east",
        "supply_hours": "4" if not day_ahead else "10",
        "n_hosts": 8,
        "reserve_cents": 100,
        "prev_base_cents": 100,
        "trailing_util_24h": "0.20",
        "boost": {"applied": "1.3", "direction": 0, "prev_trail": None},
    }
    if day_ahead:
        pool["offered_hours"] = "4"
    return {
        "round_id": "r-1",
        "round_start": "2026-09-25T00:15:00Z",
        "pools": [pool],
        "orders": [
            {
                "order_id": "E",
                "org_id": "E",
                "lottery": 1,
                "tip_cents": 10,
                "hours": "1",
                "rungs": [{"pool_id": "H", "max_cents": 500}],
            },
            {
                "order_id": "C",
                "org_id": "C",
                "lottery": 2,
                "tip_cents": 5,
                "hours": "1",
                "rungs": [{"pool_id": "H", "max_cents": 500}],
            },
            {
                "order_id": "Z",
                "org_id": "Z",
                "lottery": 3,
                "tip_cents": 0,
                "hours": "1",
                "rungs": [{"pool_id": "H", "max_cents": 50, "notify_cents": 10}],
            },
        ],
    }
