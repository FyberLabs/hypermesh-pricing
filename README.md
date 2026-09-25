# Hypermesh pricing

Stateless pricing engine for Hypermesh. It is the external service Panopticon will call. It is versioned and deployed on its own. This repository does not change Panopticon, Stripe, or a database, and it does not deploy anything.

The clearing rules follow the 2026-09-25 pricing memos. The pure functions follow the private market-sim reference (`floor.py`, `controller.py`, `orders.py`, `clearing.py`, `token_pricing.py`). Every tuning value shipped in `rulesets/` is a **simulated default** from that reference (seed 5547). None of it is a measured price, utilization, or revenue.

The platform fee (`take`) is an argument on each call. The ruleset does not contain one. It has not been decided for this service. The 8% and 12% figures discussed in the memos are not defaults here.

## Layout

| Path | Role |
|---|---|
| `pricing_core/` | Python ≥ 3.11 library. Standard library only. `Decimal` inside, integer cents at the boundary. |
| `pricing_service/` | Optional FastAPI process. Install with `pricing_core[service]`. |
| `rulesets/2026-09-25.1.json` | Immutable tuning file. A change is a new filename. |
| `openapi/openapi.json` | Published OpenAPI document for the service. |
| `tests/fixtures/` | Simulated parity fixtures. CI does not fetch market-sim. |

`pricing_core` imports cleanly on Panopticon's Python 3.13 and on the simulator's interpreter. It does not import FastAPI.

## What a round does

The percentages and multiples in this section are simulated defaults, not measured prices.

Money is integer cents. Hours and boosts are decimal strings. The same request, including lottery numbers, always returns the same response. The process stores nothing.

1. Step the conditional boost once from the boost state and `trailing_util_24h` (the 24 h trailing mean of real-time utilization **entering** the round). `b_max` is 1.3 at or below 0.35, fading linearly to 1.0 at 0.55, with hysteresis 0.03 and smoothing 0.25. A pool under the minimum host count stays at `b_max`. Static mode is that schedule with the boost held at `b_max`.
2. Set the reserve. If the pool includes an unboosted floor (`floor_usd` or `floor_cents`) and a `take`, the reserve is the ceiling of `floor × boost / (1 − take)`. Otherwise `reserve_cents` is used as given. Ceiling is what keeps a cent quote from landing below the formula.
3. Clear. Real-time is renter-proposing deferred acceptance at the posted base: one uniform base, plus that renter's own tip only when the pool is scarce. The tip is capped at 10% of base (the allowed tip is floored). Ties break by lottery (lower wins), then `order_id`. In a scarce pool one org is ranked for at most 25% of supply before anyone's over-cap hours. Day-ahead is a sealed-bid uniform price, downward-only, then one upgrade pass. Day-ahead supply is clamped to 75% of `offered_hours`.
4. Step the base at most δ = 0.025 toward utilization 0.70. The next base is clamped to the reserve and to `max(κ × reserve, λ × day-ahead median)` with κ = 3 and λ = 1.5. A thin pool (fewer than 3 hosts) is pinned at the reserve. A base already above the cap is not rewritten inside the round; the cap binds on `next_base_cents`.

Pain thresholds, when present on a rung, are applied before clearing against the lagged EMA (`ema_cents`, or `prev_base_cents` when the EMA is omitted). Notify does not change the max. The response `pain_by_order` is the memory to send next round.

## How Panopticon calls a 15-minute round

Panopticon keeps, per pool, the last base, the boost object, the lagged EMA, and the trailing 24 h real-time utilization. It does not ask this service to remember them.

Before the round closes:

1. Optional: `POST /v1/floors` with kW, tariff, overhead, capex, `u`, `take`, and a boost, when the physical floor inputs changed. The response `floor_usd` is the unboosted floor to send on the round.
2. `POST /v1/rounds/realtime` with `round_id`, `round_start`, the pinned `ruleset_version`, pools, and orders. Send the boost object from the previous response unchanged, and the new `trailing_util_24h`. Include `floor_usd` and `take` when this service should recompute the reserve from the boost it just stepped. If those are omitted, `reserve_cents` must already be the reserve for this round (from `/v1/floors` or from the pinned library).
3. Persist `next_base_cents`, `boost`, `next_ema_cents`, `reserve_cents`, and the stamped `ruleset_version` / `engine_version`. The next round's `prev_base_cents` is this round's `next_base_cents`.

Day-ahead is `POST /v1/rounds/day-ahead` once per block. Each pool must include `offered_hours`. Supply above 75% of that offer is clamped. Unsold day-ahead hours are Panopticon's to add into the real-time `supply_hours`; this service does not keep them.

Auth is `Authorization: Bearer $PRICING_SERVICE_TOKEN`. `GET /healthz` does not require it. `GET /v1/rulesets` lists published versions and their sha256 for operators. Dashboards use the endpoints in the transparency contract below, not that list: its `source` string names the simulator and is not customer copy.

## Transparency contract

Renter and host dashboards show the rules that are actually in force.

- Display the active ruleset version from `GET /v1/rulesets/active`, its `public_summary`, and only parameters with `customer_visible: true`. `GET /v1/rulesets/{version}` is the same document for one published version, including one that has been announced and is not yet active.
- The summary lines are generated from the parameter values and stored in the ruleset file. A file whose lines do not match those values is rejected, so the sentences cannot drift from the knobs.
- Display the per-pool flags on each round: `boost_active`, `boost_multiple`, `at_cap`, `at_floor`, `scarce`, `org_cap_applied`, `degraded`, and `ruleset_version`. Those say why the live price is what it is. `at_floor` means the live price is on the floor-based reserve. `at_cap` means it is at or above the spike cap. `boost_active` means the boost multiple is above 1.
- Announce a rule change with `effective_from` before that time. Publishing the file does not make it the active ruleset. `GET /v1/rulesets/active` returns the latest published ruleset whose `effective_from` is at or before now. A round that names `ruleset_version` uses that version. A round that omits it uses the ruleset active at `round_start` (or at request time when `round_start` is omitted).
- Receipts carry `ruleset_version` (on the round and on each pool). A rule change never re-prices a cleared round. Panopticon must not resubmit a cleared `round_id` under a different ruleset.
- Do not show customers the sha256, the simulator source note, lottery numbers, or another organization's orders. The ruleset document does not contain those. The round response is internal: it includes every fill so Panopticon can settle, and the dashboard filters to the caller.

`customer_visible` on the envelope marks `version`, `effective_from`, `changelog`, `public_summary`, and `active` as safe. `sha256` is not. The tuning numbers in the summary are simulated defaults from the private market-sim reference (seed 5547). They are not measured prices.

## Degraded mode

Panopticon implements this when the service does not answer. The pricing service does not implement it; it has no memory of the last round.

- Hold the last base per pool.
- Recompute the floor and reserve locally with the **pinned** `pricing_core` (the same ruleset version stamped on the last good round).
- Clamp the base at or above that reserve.
- Mark the round degraded. Pass `degraded: true` on the local library call so each pool flag is true. The HTTP API does not accept that field: a response from the service is not a degraded round, and every pool comes back with `degraded: false`.
- Skip day-ahead.
- Never go below the floor.
- Alert after 2 degraded rounds in a row.

A degraded round still uses integer cents and still ceilings the reserve. It does not invent a new ruleset and it does not clear day-ahead against a stale book.

## Versioning

- A ruleset version is chosen per round and stamped on the response and on each pool.
- A new ruleset takes effect only at a round boundary, and not before its `effective_from`. Panopticon must not resubmit a cleared `round_id` under a different ruleset. This service cannot see that history; it will happily price whatever it is sent, and the same inputs (including an explicit `ruleset_version`) always produce the same price.
- A published ruleset file does not change. `tests/test_ruleset.py` pins the sha256 of `rulesets/2026-09-25.1.json`. A tuning change is a new file such as `rulesets/2026-09-25.2.json`, added to `PUBLISHED_VERSIONS`.
- `engine_version` is the package version (`0.1.0`). It moves when the code moves, even if the ruleset does not.

## Run

```bash
python -m pip install -e ".[service,test]"
PRICING_SERVICE_TOKEN=change-me uvicorn pricing_service.app:app --host 0.0.0.0 --port 8080
pytest
```

The service token comes from the `PRICING_SERVICE_TOKEN` environment variable only. Do not commit one.

The container is Alpine (`python:3.13-alpine`). FastAPI, Pydantic, and uvicorn publish musllinux wheels, so the image does not need a compiler.

```bash
docker build -t hypermesh-pricing:0.1.0 .
docker run --rm -p 8080:8080 -e PRICING_SERVICE_TOKEN=change-me hypermesh-pricing:0.1.0
```

CI (`.github/workflows/ci.yml`) runs pytest and a Docker image build on GitHub-hosted `ubuntu-latest` runners. There is no deploy job. The parity fixtures are already in the repository. CI does not fetch the private market-sim reference.

To regenerate those fixtures on a machine that already has market-sim:

```bash
MARKET_SIM_PATH=/path/to/market-sim python scripts/regenerate_fixtures.py
```

## License

This repository does not include a license file yet. Chris will choose one.
