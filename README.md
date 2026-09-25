# Hypermesh pricing

Stateless pricing engine for Hypermesh, version 0.1.1. It is the internal HTTP service Panopticon calls. It is versioned on its own and published as a container image. This repository does not change Panopticon, Stripe, or a database.

The clearing rules follow the 2026-09-25 pricing memos. The pure functions follow the private market-sim reference (`floor.py`, `controller.py`, `orders.py`, `clearing.py`, `token_pricing.py`). Every tuning value shipped in `pricing_core/rulesets/` is a **simulated default** from that reference (seed 5547). None of it is a measured price, utilization, or revenue.

The platform fee (`take`) is an argument on each call. The ruleset does not contain one. It has not been decided for this service. The 8% and 12% figures discussed in the memos are not defaults here.

## Layout

| Path | Role |
|---|---|
| `pricing_core/` | Python ≥ 3.11 library. Standard library only. `Decimal` inside, integer cents at the boundary. |
| `pricing_core/rulesets/` | Immutable tuning files, packaged with the library. A pip or git install finds them with no extra path. |
| `pricing_service/` | Optional FastAPI process. Install with `pricing_core[service]`. |
| `openapi/openapi.json` | Published OpenAPI document for the service. |
| `tests/fixtures/` | Simulated parity fixtures. CI does not fetch market-sim. |

`pricing_core` imports cleanly on Panopticon's Python 3.13 and on the simulator's interpreter. It does not import FastAPI.

`PRICING_RULESET_DIR` still overrides the packaged rulesets when it is set to a directory. Leave it unset in production so the installed package is the source of the files.

## Pool ids

The canonical pool id is `<class_id>@<region>`. While the market has no regions, the region is `default` (`h100@default`). `canonical_pool_id` builds that string. The engine still accepts other ids so an existing book keeps clearing.

## What a round does

The percentages and multiples in this section are simulated defaults, not measured prices.

Money is integer cents. Hours and boosts are decimal strings. The same request, including lottery numbers, always returns the same response. The process stores nothing.

1. Step the conditional boost once from the boost state and `trailing_util_24h` (the 24 h trailing mean of real-time utilization **entering** the round). `b_max` is 1.3 at or below 0.35, fading linearly to 1.0 at 0.55, with hysteresis 0.03 and smoothing 0.25. A pool under the minimum host count stays at `b_max`. Static mode is that schedule with the boost held at `b_max`. A degraded round skips this step.
2. Set the reserve. If the pool includes an unboosted floor (`floor_usd` or `floor_cents`) and a `take`, the reserve is the ceiling of `(floor × boost + processor_fixed_per_hour) / (1 − take − processor_pct)`. Otherwise `reserve_cents` is used as given. `processor_pct` and `processor_fixed_cents` default to 0. A positive fixed fee is per transaction; pass `expected_hours_per_txn` and the engine spreads it as `(processor_fixed_cents / 100) / expected_hours_per_txn` dollars per hour. Ceiling keeps the host's net at the ceiled reserve at or above `floor × boost` after the platform take and those processor fees. `supply_tiers` replaces this single reserve: see below.
3. Clear. Real-time is renter-proposing deferred acceptance at the posted base. `pay_cents` on a fill is the per-box-hour box price and does not include the tip. `tip_cents` is the per-box-hour priority tip actually charged: zero unless the pool is scarce, capped at 10% of base (the allowed tip is floored), and clipped so pay + tip never exceeds the renter's max. `tip_total_cents` is `tip_cents × hours` (half-even). `total_cents = pay_cents × hours + tip_total_cents`. Ties break by lottery (lower wins), then `order_id`. In a scarce pool one org is ranked for at most 25% of supply before anyone's over-cap hours. Day-ahead is a sealed-bid uniform price, downward-only, then one upgrade pass. Day-ahead supply is clamped to 75% of `offered_hours`. Day-ahead tips are zero, so `total_cents` is the uniform price times hours.
4. Step the base at most δ = 0.025 toward utilization 0.70. The next base is clamped to the reserve and to `max(κ × reserve, λ × day-ahead median)` with κ = 3 and λ = 1.5. A thin pool (fewer than 3 hosts) is pinned at the reserve. A base already above the cap is not rewritten inside the round; the cap binds on `next_base_cents`. A degraded round does not take this step: `next_base_cents` is the held base.

Pain thresholds, when present on a rung, are applied before clearing against the lagged EMA (`ema_cents`, or `prev_base_cents` when the EMA is omitted). Notify does not change the max. The response `pain_by_order` is the memory to send next round.

### Supply and multi-hour leases

`supply_hours` is box-hours available in the next 1 hour block: certified boxes the pool is selling, minus boxes already committed in that block. Panopticon computes that number. This service does not keep a commitment book.

A real-time fill locks its clearing price for up to `lock_hours_max` hours (ruleset `2026-09-25.2`, simulated default 24). The response field `price_lock_hours` is `min(filled hours, lock_hours_max)`. The box counts as committed in every block that lock covers. A longer lease renews at each lock boundary at the then-current price, subject to the renter's max price and pain levels. Ruleset `2026-09-25.1` has no lock knob; the engine still defaults the cap to 24 hours when the loaded file omits it.

### Per-box reserves

A pool may send `supply_tiers: [{reserve_cents, hours}, ...]` instead of one `reserve_cents` and `supply_hours`. Supply at price `p` is the sum of hours in tiers whose reserve is at or below `p`. The pool base is clamped at or above the cheapest tier. No box is allocated below its own reserve, so no host is paid below its floor. The single-reserve form is unchanged when `supply_tiers` is omitted. When both are sent, the tiers win: each tier's `reserve_cents` is already that box's reserve.

### Lottery

The caller stores one secret `round_seed` per round. Before clearing, publish `sha256(round_seed)`. After clearing, reveal the seed. Renters audit a tie-break with `derive_lottery(round_seed, order_id)`, which is HMAC-SHA256 of the order id under the seed, first 8 bytes as a big-endian integer. Lower wins, then `order_id`. The engine does not store the seed. Send the derived integers on the orders.

### Request hash

Every round and floor response includes `request_hash`, the sha256 of the canonical JSON body (sorted keys, no insignificant whitespace). An optional `Idempotency-Key` header is echoed in the JSON and as a response header. It is not part of the hash. The service stays stateless. Determinism makes a retry of the same body safe.

## How Panopticon calls a 15-minute round

Panopticon keeps, per pool, the last base, the boost object, the lagged EMA, and the trailing 24 h real-time utilization. It does not ask this service to remember them.

Client budget: 2 seconds timeout per call, up to 3 attempts with jittered backoff, and 90 seconds total per round. After that budget, run `pricing_core.degraded_round(...)` locally. Do not keep retrying.

Before the round closes:

1. Optional: `POST /v1/floors` with kW, tariff, overhead, capex, `u`, `take`, and a boost, when the physical floor inputs changed. Add `processor_pct`, `processor_fixed_cents`, and `expected_hours_per_txn` when the host must clear processor fees. The response `floor_usd` is the unboosted floor to send on the round. `reserve_cents` is the ceiled gross-up.
2. `POST /v1/rounds/realtime` with `round_id`, `round_start`, the pinned `ruleset_version`, pools, and orders. Send the boost object from the previous response unchanged, and the new `trailing_util_24h`. Include `floor_usd` and `take` when this service should recompute the reserve from the boost it just stepped. If those are omitted, `reserve_cents` must already be the reserve for this round (from `/v1/floors` or from the pinned library). Pass the same processor fields on the round or on a pool when the formula should include them.
3. Optional: `POST /v1/token-prices` turns a pool's current `base_cents` and `reserve_cents` into per-1,000-token input and output prices and a loaded-hour minimum (`loaded_hour_min_cents`, the reserve). P2 is priced per model from that model's class pool. Pass that product's own `take`. Throughput (`input_tokens_per_second`, `output_tokens_per_second`, `u_batch`) is an argument. Dynamic per-token clearing is out of scope. The numbers are not measured tokens/s.
4. Persist `next_base_cents`, `boost`, `next_ema_cents`, `reserve_cents`, and the stamped `ruleset_version` / `engine_version`. The next round's `prev_base_cents` is this round's `next_base_cents`. Stamp `request_hash` on the receipt if you want a retry check.

Day-ahead is `POST /v1/rounds/day-ahead` once per block. Each pool must include `offered_hours`. Supply above 75% of that offer is clamped. Unsold day-ahead hours are Panopticon's to add into the real-time `supply_hours`; this service does not keep them. A degraded round refuses day-ahead.

Auth is `Authorization: Bearer $PRICING_SERVICE_TOKEN` on rounds, floors, and token prices. `GET /healthz` does not require it. `GET /v1/rulesets`, `GET /v1/rulesets/active`, and `GET /v1/rulesets/{version}` are also unauthenticated. They return customer-safe fields only: version, effective time, changelog, public summary, and parameters a dashboard may render. They do not return the sha256, the simulator source note, or internal knobs. Panopticon should still proxy those three routes through its public edge rather than exposing this process directly.

## Transparency contract

Renter and host dashboards show the rules that are actually in force.

- Display the active ruleset version from `GET /v1/rulesets/active`, its `public_summary`, and the parameters in that response. `GET /v1/rulesets/{version}` is the same document for one published version, including one that has been announced and is not yet active. Proxy these through Panopticon's public edge.
- The summary lines are generated from the parameter values and stored in the ruleset file. A file whose lines do not match those values is rejected, so the sentences cannot drift from the knobs. The lock line ("A real-time fill locks its price for up to 24 hours") is on ruleset `2026-09-25.2`.
- Display the per-pool flags on each round: `boost_active`, `boost_multiple`, `at_cap`, `at_floor`, `scarce`, `org_cap_applied`, `degraded`, and `ruleset_version`. Those say why the live price is what it is. `at_floor` means the live price is on the floor-based reserve. `at_cap` means it is at or above the spike cap. `boost_active` means the boost multiple is above 1.
- Announce a rule change with `effective_from` before that time. Publishing the file does not make it the active ruleset. `GET /v1/rulesets/active` returns the latest published ruleset whose `effective_from` is at or before now. A round that names `ruleset_version` uses that version. A round that omits it uses the ruleset active at `round_start` (or at request time when `round_start` is omitted).
- Receipts carry `ruleset_version` (on the round and on each pool). A rule change never re-prices a cleared round. Panopticon must not resubmit a cleared `round_id` under a different ruleset.
- Do not show customers the sha256, the simulator source note, lottery seeds, or another organization's orders. The public ruleset document does not contain those. The round response is internal: it includes every fill so Panopticon can settle, and the dashboard filters to the caller.

The tuning numbers in the summary are simulated defaults from the private market-sim reference (seed 5547). They are not measured prices.

## Degraded mode

When the service does not answer inside the client budget above, Panopticon prices the round locally:

```python
from pricing_core.engine import degraded_round
result = degraded_round(payload)
```

The same flag is accepted on `POST /v1/rounds/realtime` as `"degraded": true`. In a degraded round:

- `next_base_cents` is the base held for the round, clamped up to the reserve. It is not stepped.
- Boost state is not advanced, and the utilization EMA is not advanced. Send the same boost object and the same trailing utilization next time; do not roll the 24 h window for a round that did not complete.
- Spot orders still clear at that held price, clamped at or above the reserve, with the normal ladder, lottery, and org cap.
- Every pool is flagged `degraded: true`.
- Day-ahead is refused.

A degraded round still uses integer cents and still ceilings the reserve. It does not invent a new ruleset. Alert after 2 degraded rounds in a row.

## Versioning

- Engine version `0.1.1`. A ruleset version is chosen per round and stamped on the response and on each pool.
- A new ruleset takes effect only at a round boundary, and not before its `effective_from`. Panopticon must not resubmit a cleared `round_id` under a different ruleset. This service cannot see that history; it will happily price whatever it is sent, and the same inputs (including an explicit `ruleset_version`) always produce the same price.
- A published ruleset file does not change. `tests/test_ruleset.py` pins the sha256 of `pricing_core/rulesets/2026-09-25.1.json` and of `2026-09-25.2.json`. A tuning change is a new file, added to `PUBLISHED_VERSIONS`. `2026-09-25.2` adds `market.lock_hours_max` (24). It does not edit `.1`.
- `engine_version` is the package version (`0.1.1`). It moves when the code moves, even if the ruleset does not.
- The `v0.1.1` git tag is cut after this release is merged. Pushing that tag publishes the image. Do not point production at a moving tag. Pin the digest the release workflow prints.

## Images

`.github/workflows/release.yml` builds and pushes `ghcr.io/fyberlabs/hypermesh-pricing` from a GitHub-hosted `ubuntu-latest` runner, using `GITHUB_TOKEN` with `packages: write`.

| Git push | Image tags |
|---|---|
| `v*` tag, for example `v0.1.1` | `ghcr.io/fyberlabs/hypermesh-pricing:v0.1.1` |
| branch `main` | `ghcr.io/fyberlabs/hypermesh-pricing:main` and `ghcr.io/fyberlabs/hypermesh-pricing:sha-<full commit sha>` |

The job prints the image digest. Pin the running image by digest:

```text
ghcr.io/fyberlabs/hypermesh-pricing@sha256:<digest>
```

The tag can be moved. The digest cannot. There is no deploy job in this repository. Panopticon pulls the pinned image and runs it.

A new GHCR package is private until its visibility is set. The release job tries to mark it public and continues if that call fails. `GITHUB_TOKEN` usually cannot change org package visibility. The one-time fix is to set the package public in the organization package settings, or link the package to this repository. The image does not contain `PRICING_SERVICE_TOKEN`.

## Run

The process listens on `0.0.0.0:8080`. `GET /healthz` does not require a bearer token. Rounds, floors, and token prices do: `Authorization: Bearer $PRICING_SERVICE_TOKEN`.

| Variable | Required | Meaning |
|---|---|---|
| `PRICING_SERVICE_TOKEN` | yes | Bearer token for `/v1` pricing calls. The process exits before it listens when this is unset, empty, or only whitespace. It is not written into the image and it is not logged. |
| `PRICING_RULESET_DIR` | no | Directory that replaces the packaged rulesets. Leave it unset in production so the image uses the files shipped in `pricing_core`. |

Do not commit a real token.

From a checkout, for local development:

```bash
python -m pip install -e ".[service,test]"
PRICING_SERVICE_TOKEN=change-me python -m pricing_service
pytest
```

`python -m pricing_service` is the container command. Replace `change-me` with a token you supply at runtime.

The container is Alpine (`python:3.13-alpine`), runs as user `pricing` (uid 10001), and does not need a compiler: FastAPI, Pydantic, and uvicorn publish musllinux wheels. Rulesets are inside the `pricing_core` package.

```bash
docker build -t hypermesh-pricing:local .
docker run --rm -p 8080:8080 -e PRICING_SERVICE_TOKEN=change-me hypermesh-pricing:local
curl -fsS http://127.0.0.1:8080/healthz
```

Omitting `PRICING_SERVICE_TOKEN` makes that `docker run` exit before it binds to port 8080.

Panopticon runs the published image as an internal compose service and pins the digest:

```yaml
services:
  pricing:
    image: ghcr.io/fyberlabs/hypermesh-pricing@sha256:<digest>
    environment:
      PRICING_SERVICE_TOKEN: ${PRICING_SERVICE_TOKEN}
    expose:
      - "8080"
```

CI (`.github/workflows/ci.yml`) runs pytest and builds the image, then checks that a missing token exits and that `/healthz` answers, on GitHub-hosted `ubuntu-latest` runners. There is no deploy job. The parity fixtures are already in the repository. CI does not fetch the private market-sim reference.

To regenerate those fixtures on a machine that already has market-sim:

```bash
MARKET_SIM_PATH=/path/to/market-sim python scripts/regenerate_fixtures.py
```

## License

MIT. Copyright 2026 Fyber Labs. See `LICENSE`.
