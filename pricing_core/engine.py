"""Stateless round orchestration.

One call prices one round. Nothing is stored. The boost state and the EMA
in the response are what the caller sends back on the next round.

Reserve rule: the engine steps the boost once from the state and the
trailing utilization in the request, unless the round is degraded. If the
pool carries an unboosted floor and a ``take``, the reserve is the ceiling of
``(floor × boost + processor_fixed_per_hour) / (1 − take − processor_pct)``.
Processor inputs default to zero. Otherwise ``reserve_cents`` is the reserve,
or ``supply_tiers`` replaces that single reserve. Pass the boost object from
the previous round unchanged.

A degraded round holds the posted base (clamped up to the reserve), does not
advance boost or the utilization EMA, still clears spot orders, and refuses
day-ahead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from pricing_core import ENGINE_VERSION
from pricing_core.boost import BoostState, step_boost
from pricing_core.clearing import (
    PoolView,
    clear_day_ahead,
    clear_realtime,
    commit_upgrade_pass,
)
from pricing_core.controller import corridor_cap_cents, update_base_cents, update_ema_cents
from pricing_core.floor import fixed_fee_per_hour, quote_floor, reserve_cents
from pricing_core.hashing import request_hash
from pricing_core.money import ceil_cents, cents_to_dollars, dec_str, parse_decimal, scale_cents
from pricing_core.orders import Order, PainState, Rung, apply_pain
from pricing_core.ruleset import Ruleset, RulesetError, active_version, load_ruleset
from pricing_core.token_pricing import price_per_thousand_cents, reference_cents

class EngineError(ValueError):
    """The round request is not a valid input for this ruleset."""


def price_floor(payload: dict, ruleset: Ruleset | None = None) -> dict:
    rules = _rules(payload, ruleset)
    loaded_kw = _required_decimal(payload, "loaded_kw")
    tariff = _required_decimal(payload, "tariff_per_kwh")
    overhead = _required_decimal(payload, "overhead")
    capex = _required_decimal(payload, "capex")
    if "take" not in payload or payload["take"] is None:
        raise EngineError("take is required; the engine has no default platform fee")
    take = parse_decimal(payload["take"])
    if "boost" not in payload or payload["boost"] is None:
        raise EngineError("boost is required")
    boost = parse_decimal(payload["boost"])
    residual = _optional_decimal(payload, "residual", rules.floor["residual_default"])
    utilization = _optional_decimal(payload, "u", rules.floor["u_default"])
    host_margin = _optional_decimal(payload, "host_margin", "0")
    processor_pct, fixed_per_hour, fixed_cents, expected_hours = _processor(payload)
    try:
        quote = quote_floor(
            loaded_kw=loaded_kw,
            tariff_per_kwh=tariff,
            overhead=overhead,
            capex=capex,
            residual=residual,
            utilization=utilization,
            host_margin=host_margin,
            take=take,
            boost=boost,
            processor_pct=processor_pct,
            processor_fixed_per_hour=fixed_per_hour,
            years=int(rules.floor["depreciation_years"]),
            hours_per_year=int(rules.floor["hours_per_year"]),
            u_min=rules.dec("floor", "u_min"),
            u_max=rules.dec("floor", "u_max"),
        )
    except (TypeError, ValueError) as exc:
        raise EngineError(str(exc)) from exc
    body = {
        "engine_version": ENGINE_VERSION,
        "ruleset_version": rules.version,
        "floor_cents": quote["floor_cents"],
        "reserve_cents": quote["reserve_cents"],
        "floor_usd": dec_str(quote["floor"]),
        "reserve_usd": dec_str(quote["reserve"]),
        "u_used": dec_str(quote["u_used"]),
        "boost_used": dec_str(boost),
        "processor_pct": dec_str(processor_pct),
        "processor_fixed_cents": fixed_cents,
        "expected_hours_per_txn": None if expected_hours is None else dec_str(expected_hours),
    }
    return _stamp(payload, body)


def price_realtime(payload: dict, ruleset: Ruleset | None = None) -> dict:
    return _price_round(payload, ruleset, day_ahead=False)


def degraded_round(payload: dict, ruleset: Ruleset | None = None) -> dict:
    """Price one real-time round in degraded mode.

    Panopticon calls this locally when the service does not answer. The HTTP
    API accepts the same ``degraded: true`` flag on ``POST /v1/rounds/realtime``.
    """
    if not isinstance(payload, dict):
        raise EngineError("round payload must be an object")
    body = dict(payload)
    body["degraded"] = True
    return price_realtime(body, ruleset)


def price_day_ahead(payload: dict, ruleset: Ruleset | None = None) -> dict:
    return _price_round(payload, ruleset, day_ahead=True)


def price_tokens(payload: dict, ruleset: Ruleset | None = None) -> dict:
    """Per-1k input and output prices from a class pool's current hour.

    Dynamic per-token clearing is out of scope. P2 prices each model from
    that model's class pool and passes its own ``take``. The per-1k quotes
    are ceiled from the hourly reference grossed up by that take, so a loaded
    hour at ``u_batch`` covers the reference after the platform fee.
    ``loaded_hour_min_cents`` is the pool reserve: no host hour is priced
    below it. Throughput is an argument, not a measured tokens/s figure.
    """
    if not isinstance(payload, dict):
        raise EngineError("token price payload must be an object")
    rules = _rules(payload, ruleset)
    if payload.get("take") is None:
        raise EngineError("take is required; the engine has no default platform fee")
    try:
        take = parse_decimal(payload["take"])
    except (TypeError, ValueError) as exc:
        raise EngineError(str(exc)) from exc
    if not (Decimal(0) <= take < 1):
        raise EngineError("take must be in [0, 1)")
    base = _required_cents(payload, "base_cents")
    reserve = _required_cents(payload, "reserve_cents")
    reference = reference_cents(reserve, base)
    try:
        billed = ceil_cents((Decimal(reference) / Decimal(100)) / (Decimal(1) - take))
        u_batch = _optional_decimal(payload, "u_batch", "1")
        input_tps = _required_decimal(payload, "input_tokens_per_second")
        output_tps = _required_decimal(payload, "output_tokens_per_second")
        input_per_1k = price_per_thousand_cents(billed, input_tps, u_batch)
        output_per_1k = price_per_thousand_cents(billed, output_tps, u_batch)
    except (TypeError, ValueError) as exc:
        raise EngineError(str(exc)) from exc
    body: dict = {
        "engine_version": ENGINE_VERSION,
        "ruleset_version": rules.version,
        "take": dec_str(take),
        "base_cents": base,
        "reserve_cents": reserve,
        "reference_cents": reference,
        "billed_hour_cents": billed,
        "input_per_1k_cents": input_per_1k,
        "output_per_1k_cents": output_per_1k,
        "loaded_hour_min_cents": reserve,
        "u_batch": dec_str(u_batch),
    }
    for key in ("pool_id", "class_id", "model_id"):
        if payload.get(key) not in (None, ""):
            body[key] = _required_str(payload, key)
    return _stamp(payload, body)


def _price_round(payload: dict, ruleset: Ruleset | None, *, day_ahead: bool) -> dict:
    rules = _rules(payload, ruleset)
    round_id = _required_str(payload, "round_id")
    _parse_round_start(payload.get("round_start"))
    degraded = _degraded_flag(payload)
    if day_ahead and degraded:
        raise EngineError("day-ahead is refused on a degraded round")
    pools_in = payload.get("pools")
    orders_in = payload.get("orders")
    if not isinstance(pools_in, list) or not pools_in:
        raise EngineError("pools must be a non-empty list")
    if not isinstance(orders_in, list):
        raise EngineError("orders must be a list")

    processor = _processor(payload)
    prepared = [
        _prepare_pool(
            raw, rules, day_ahead=day_ahead, degraded=degraded, processor=processor
        )
        for raw in pools_in
    ]
    ids = [item["pool_id"] for item in prepared]
    if len(ids) != len(set(ids)):
        raise EngineError("duplicate pool_id")
    by_id = {item["pool_id"]: item for item in prepared}

    built, pain_out = _prepare_orders(orders_in, by_id, rules, day_ahead=day_ahead)
    share = rules.dec("market", "share_cap")
    tip = rules.dec("market", "tip_cap")
    max_passes = int(rules.market["max_passes"])
    views, result, gains = _clear_prepared(
        prepared,
        built,
        rules,
        share=share,
        tip=tip,
        max_passes=max_passes,
        day_ahead=day_ahead,
    )

    pool_rows = []
    for item in sorted(prepared, key=lambda row: row["pool_id"]):
        stats = result.pools[item["pool_id"]]
        if degraded:
            nxt = item["posted_base_cents"]
            util = stats.utilization
        else:
            nxt, util, _updated = update_base_cents(
                item["posted_base_cents"],
                stats.demand,
                stats.supply,
                item["n_hosts"],
                target=rules.dec("controller", "target_util"),
                delta=rules.dec("controller", "delta"),
                reserve_cents=item["reserve_cents"],
                cap_cents=item["cap_cents"],
                min_hosts=int(rules.boost["min_hosts"]),
            )
        scarce = stats.scarce
        pool_rows.append(
            {
                "pool_id": item["pool_id"],
                "class_id": item["class_id"],
                "region": item["region"],
                "base_cents": item["posted_base_cents"],
                "next_base_cents": nxt,
                "reserve_cents": item["reserve_cents"],
                "cap_cents": item["cap_cents"],
                "utilization": dec_str(util),
                "scarce": scarce,
                "org_cap_applied": bool(stats.org_cap_applied),
                "rationed": bool(scarce and item["posted_base_cents"] >= item["cap_cents"]),
                "boost_active": item["boost"].applied > Decimal(1),
                "boost_multiple": dec_str(item["boost"].applied),
                "at_cap": item["posted_base_cents"] >= item["cap_cents"],
                "at_floor": item["posted_base_cents"] == item["reserve_cents"],
                "degraded": degraded,
                "ruleset_version": rules.version,
                "demand_hours": dec_str(stats.demand),
                "supply_hours": dec_str(stats.supply),
                "supply_clamped": item["supply_clamped"],
                "reserve_source": item["reserve_source"],
                "boost": item["boost"].as_dict(),
                "next_ema_cents": item["next_ema_cents"],
            }
        )
        if pool_rows[-1]["base_cents"] < pool_rows[-1]["reserve_cents"]:
            raise EngineError("internal: posted base fell below the reserve")
        if pool_rows[-1]["next_base_cents"] < pool_rows[-1]["reserve_cents"]:
            raise EngineError("internal: next base fell below the reserve")

    filled_hours: dict[str, Decimal] = {}
    fills = []
    for fill in result.fills:
        if fill.pay_cents < views[fill.pool_id].reserve_cents:
            raise EngineError("internal: fill priced below the reserve")
        filled_hours[fill.order_id] = filled_hours.get(fill.order_id, Decimal(0)) + fill.hours
        box_total = scale_cents(fill.pay_cents, fill.hours)
        tip_total = scale_cents(fill.tip_cents, fill.hours)
        lock_hours = None
        if not day_ahead:
            lock_hours = dec_str(min(fill.hours, Decimal(_lock_hours_max(rules))))
        fills.append(
            {
                "order_id": fill.order_id,
                "org_id": fill.org_id,
                "pool_id": fill.pool_id,
                "rung_index": fill.rung_index,
                "hours": dec_str(fill.hours),
                "pay_cents": fill.pay_cents,
                "tip_cents": fill.tip_cents,
                "tip_total_cents": tip_total,
                "total_cents": box_total + tip_total,
                "price_lock_hours": lock_hours,
            }
        )
    unfilled = []
    for order in built:
        left = order.hours - filled_hours.get(order.order_id, Decimal(0))
        if left > Decimal("1e-9"):
            unfilled.append({"order_id": order.order_id, "org_id": order.org_id, "hours": dec_str(left)})
    # Orders removed by pain (stop) were not in `built`.
    for order_id, meta in pain_out.items():
        if meta["dropped"] and order_id not in {row["order_id"] for row in unfilled}:
            unfilled.append(
                {"order_id": order_id, "org_id": meta["org_id"], "hours": dec_str(meta["hours"])}
            )
    unfilled.sort(key=lambda row: row["order_id"])

    body = {
        "engine_version": ENGINE_VERSION,
        "ruleset_version": rules.version,
        "round_id": round_id,
        "round_start": payload.get("round_start"),
        "passes": result.passes,
        "hit_iteration_cap": result.hit_iteration_cap,
        "pools": pool_rows,
        "fills": fills,
        "unfilled": unfilled,
        "pain_by_order": {order_id: meta["pain"] for order_id, meta in sorted(pain_out.items())},
    }
    body["price_lock_hours_max"] = _lock_hours_max(rules)
    if day_ahead:
        body["upgrade_applied"] = bool(rules.market["day_ahead_upgrade"])
        body["upgrade_gains"] = gains
    return _stamp(payload, body)


def _clear_prepared(prepared, built, rules, *, share, tip, max_passes, day_ahead):
    """Clear, shrinking tiered day-ahead supply until it matches the price."""
    if day_ahead and any(item["tiers"] for item in prepared):
        result = None
        for _ in range(16):
            views = _views(prepared)
            result = clear_day_ahead(built, views, share_cap=share, max_passes=max_passes)
            changed = False
            for item in prepared:
                if not item["tiers"]:
                    continue
                price = result.final_price_cents.get(item["pool_id"])
                if price is None:
                    price = item["reserve_cents"]
                new_supply, clamped = _tier_supply(item, int(price), rules, day_ahead=True)
                if new_supply != item["supply"] or clamped != item["supply_clamped"]:
                    item["supply"] = new_supply
                    item["supply_clamped"] = clamped
                    changed = True
            if not changed:
                break
        views = _views(prepared)
        result = clear_day_ahead(built, views, share_cap=share, max_passes=max_passes)
    else:
        views = _views(prepared)
        if day_ahead:
            result = clear_day_ahead(built, views, share_cap=share, max_passes=max_passes)
        else:
            result = clear_realtime(built, views, share_cap=share, tip_cap=tip, max_passes=max_passes)
    if day_ahead and rules.market["day_ahead_upgrade"] and built:
        result, gains = commit_upgrade_pass(built, result, views, share)
    else:
        gains = 0
    return views, result, gains


def _views(prepared) -> dict:
    return {
        item["pool_id"]: PoolView(
            pool_id=item["pool_id"],
            supply=item["supply"],
            base_cents=item["posted_base_cents"],
            reserve_cents=item["reserve_cents"],
        )
        for item in prepared
    }


def _prepare_pool(
    raw: object,
    rules: Ruleset,
    *,
    day_ahead: bool,
    degraded: bool,
    processor: tuple[Decimal, Decimal, int, Decimal | None],
) -> dict:
    if not isinstance(raw, dict):
        raise EngineError("pool must be an object")
    pool_id = _required_str(raw, "pool_id")
    class_id = _required_str(raw, "class_id")
    region = _required_str(raw, "region")
    tiers = _parse_tiers(raw, pool_id)
    if tiers is None:
        supply = _required_decimal(raw, "supply_hours")
        if supply < 0:
            raise EngineError(f"{pool_id}: supply_hours must be >= 0")
    else:
        supply = sum((hours for _reserve, hours in tiers), Decimal(0))
    n_hosts = raw.get("n_hosts")
    if isinstance(n_hosts, bool) or not isinstance(n_hosts, int) or n_hosts < 0:
        raise EngineError(f"{pool_id}: n_hosts must be a non-negative integer")
    if tiers is None:
        reserve_in = _required_cents(raw, "reserve_cents")
    else:
        reserve_in = min(reserve for reserve, _hours in tiers)
    prev_base = _required_cents(raw, "prev_base_cents")
    trailing = _required_decimal(raw, "trailing_util_24h")
    if trailing < 0:
        raise EngineError(f"{pool_id}: trailing_util_24h must be >= 0")
    median = raw.get("day_ahead_median_7d_cents")
    if median is not None:
        median = _as_cents(median, f"{pool_id}: day_ahead_median_7d_cents")
    boost_state = _parse_boost(raw.get("boost"), rules, pool_id)
    if degraded:
        # Hold the boost that entered the round. Do not record this round's
        # trailing utilization as a new observation.
        stepped = boost_state
    else:
        stepped = step_boost(
            boost_state,
            trailing,
            n_hosts=n_hosts,
            mode=str(rules.boost["mode"]),
            b_max=rules.dec("boost", "b_max"),
            u_low=rules.dec("boost", "u_low"),
            u_high=rules.dec("boost", "u_high"),
            hysteresis=rules.dec("boost", "hysteresis"),
            smooth=rules.dec("boost", "smooth"),
            min_hosts=int(rules.boost["min_hosts"]),
            thin_plateau=bool(rules.boost["thin_plateau"]),
        )
    if tiers is None:
        reserve, reserve_source = _reserve_for_pool(
            raw, reserve_in, stepped, pool_id, _processor_for_pool(raw, processor)
        )
    else:
        # Tier reserves are already per box. They replace the single floor formula.
        reserve, reserve_source = reserve_in, "tiers"
    cap = corridor_cap_cents(
        reserve,
        median,
        rules.dec("controller", "kappa"),
        rules.dec("controller", "lambda"),
    )
    # The cap binds on the controller's next base. The posted base is the
    # price already in force: it is lifted to the reserve, and a thin pool
    # is pinned there, but it is not rewritten down to the cap inside the
    # round that was given that base. A degraded round only clamps up.
    if degraded or n_hosts >= int(rules.boost["min_hosts"]):
        posted = max(prev_base, reserve)
    else:
        posted = reserve
    if tiers is not None and not day_ahead:
        supply, clamped = _tier_supply(
            {"tiers": tiers, "offered_raw": raw}, posted, rules, day_ahead=False
        )
    else:
        supply, clamped = _clamp_day_ahead(raw, supply, rules, day_ahead=day_ahead, pool_id=pool_id)
    ema_in = raw.get("ema_cents")
    lagged = _as_cents(ema_in, f"{pool_id}: ema_cents") if ema_in is not None else prev_base
    if degraded:
        next_ema = lagged
    else:
        next_ema = update_ema_cents(lagged, posted, rules.dec("market", "smoothing_alpha"))
    return {
        "pool_id": pool_id,
        "class_id": class_id,
        "region": region,
        "supply": supply,
        "supply_clamped": clamped,
        "tiers": tiers,
        "offered_raw": raw,
        "n_hosts": n_hosts,
        "reserve_cents": reserve,
        "reserve_source": reserve_source,
        "posted_base_cents": posted,
        "cap_cents": cap,
        "boost": stepped,
        "lagged_ema_cents": lagged,
        "next_ema_cents": next_ema,
    }


def _reserve_for_pool(
    raw: dict,
    reserve_in: int,
    stepped: BoostState,
    pool_id: str,
    processor: tuple[Decimal, Decimal, int, Decimal | None],
) -> tuple[int, str]:
    """Reserve in force for this round.

    When the caller supplies an unboosted floor and a take, the formula
    wins: ``ceil((floor × boost + processor_fixed_per_hour) / (1 − take − processor_pct))``.
    Processor inputs default to zero. ``reserve_cents`` alone is the reserve
    when no floor is supplied (the caller already applied the boost, for
    example via ``POST /v1/floors``).
    """
    take_raw = raw.get("take")
    floor_usd = raw.get("floor_usd")
    floor_cents = raw.get("floor_cents")
    if floor_usd is None and floor_cents is None:
        return reserve_in, "request"
    if take_raw is None:
        raise EngineError(f"{pool_id}: take is required when a floor is supplied")
    try:
        take = parse_decimal(take_raw)
        if floor_usd is not None:
            floor = parse_decimal(floor_usd)
        else:
            floor = cents_to_dollars(_as_cents(floor_cents, f"{pool_id}: floor_cents"))
        pct, per_hour, _fixed, _hours = processor
        quoted = reserve_cents(
            floor,
            take,
            stepped.applied,
            processor_pct=pct,
            processor_fixed_per_hour=per_hour,
        )
    except (TypeError, ValueError) as exc:
        raise EngineError(f"{pool_id}: {exc}") from exc
    return quoted, "floor"


def _parse_tiers(raw: dict, pool_id: str) -> list[tuple[int, Decimal]] | None:
    """Per-box reserves. Absent means the single ``reserve_cents`` form."""
    if "supply_tiers" not in raw or raw["supply_tiers"] is None:
        return None
    tiers = raw["supply_tiers"]
    if not isinstance(tiers, list) or not tiers:
        raise EngineError(f"{pool_id}: supply_tiers must be a non-empty list")
    parsed: list[tuple[int, Decimal]] = []
    for index, item in enumerate(tiers):
        if not isinstance(item, dict):
            raise EngineError(f"{pool_id}: supply_tiers[{index}] must be an object")
        reserve = _as_cents(item.get("reserve_cents"), f"{pool_id}: supply_tiers[{index}].reserve_cents")
        if "hours" not in item or item["hours"] is None:
            raise EngineError(f"{pool_id}: supply_tiers[{index}].hours is required")
        try:
            hours = parse_decimal(item["hours"])
        except (TypeError, ValueError) as exc:
            raise EngineError(f"{pool_id}: supply_tiers[{index}].hours is invalid") from exc
        if hours <= 0:
            raise EngineError(f"{pool_id}: supply_tiers[{index}].hours must be positive")
        parsed.append((reserve, hours))
    return parsed


def _tier_supply(
    item: dict, price: int, rules: Ruleset, *, day_ahead: bool
) -> tuple[Decimal, bool]:
    """Hours whose own reserve is at or below ``price``.

    A box is never offered below its reserve, so supply at ``price`` is the
    sum of those tiers. Day-ahead then applies the offered-hours share cap.
    """
    if isinstance(price, bool) or not isinstance(price, int):
        price = int(price)
    supply = sum((hours for reserve, hours in item["tiers"] if reserve <= price), Decimal(0))
    if not day_ahead:
        return supply, False
    raw = item["offered_raw"]
    pool_id = item.get("pool_id") or raw.get("pool_id", "pool")
    return _clamp_day_ahead(raw, supply, rules, day_ahead=True, pool_id=str(pool_id))


def _lock_hours_max(rules: Ruleset) -> int:
    if "lock_hours_max" in rules.market:
        return int(rules.market["lock_hours_max"])
    return 24


def _processor(payload: dict) -> tuple[Decimal, Decimal, int, Decimal | None]:
    """``(processor_pct, fixed dollars per hour, fixed cents, expected hours)``.

    Defaults are zero. A positive per-transaction fee needs
    ``expected_hours_per_txn`` so it can be spread across the hours that
    transaction covers: ``processor_fixed_per_hour = (fixed_cents / 100) / hours``.
    """
    try:
        pct_raw = payload.get("processor_pct", 0)
        if pct_raw is None:
            pct_raw = 0
        pct = parse_decimal(pct_raw)
        fixed_raw = payload.get("processor_fixed_cents", 0)
        if fixed_raw is None:
            fixed_raw = 0
        if isinstance(fixed_raw, bool) or not isinstance(fixed_raw, int) or fixed_raw < 0:
            raise EngineError("processor_fixed_cents must be a non-negative integer")
        hours_raw = payload.get("expected_hours_per_txn")
        hours = None if hours_raw is None else parse_decimal(hours_raw)
        per_hour = fixed_fee_per_hour(fixed_raw, hours)
    except EngineError:
        raise
    except (TypeError, ValueError) as exc:
        raise EngineError(str(exc)) from exc
    if not (Decimal(0) <= pct < 1):
        raise EngineError("processor_pct must be in [0, 1)")
    return pct, per_hour, fixed_raw, hours


def _processor_for_pool(
    raw: dict, fallback: tuple[Decimal, Decimal, int, Decimal | None]
) -> tuple[Decimal, Decimal, int, Decimal | None]:
    keys = ("processor_pct", "processor_fixed_cents", "expected_hours_per_txn")
    if not any(raw.get(key) is not None for key in keys):
        return fallback
    pct, _per_hour, fixed_cents, hours = fallback
    merged = {
        "processor_pct": pct if raw.get("processor_pct") is None else raw.get("processor_pct"),
        "processor_fixed_cents": fixed_cents
        if raw.get("processor_fixed_cents") is None
        else raw.get("processor_fixed_cents"),
        "expected_hours_per_txn": hours
        if raw.get("expected_hours_per_txn") is None
        else raw.get("expected_hours_per_txn"),
    }
    return _processor(merged)


def _stamp(payload: dict, body: dict) -> dict:
    """Attach the canonical request hash and echo an idempotency key.

    The key is not part of the hash. The service stores nothing; the same
    body always prices the same, so a retry is safe.
    """
    stamped = dict(body)
    stamped["request_hash"] = request_hash(payload)
    key = payload.get("idempotency_key")
    if key not in (None, ""):
        if not isinstance(key, str):
            raise EngineError("idempotency_key must be a string")
        stamped["idempotency_key"] = key
    return stamped


def _clamp_day_ahead(
    raw: dict,
    supply: Decimal,
    rules: Ruleset,
    *,
    day_ahead: bool,
    pool_id: str,
) -> tuple[Decimal, bool]:
    if not day_ahead:
        return supply, False
    if "offered_hours" not in raw or raw["offered_hours"] is None:
        raise EngineError(
            f"{pool_id}: day-ahead pools require offered_hours so the "
            f"{rules.market['day_ahead_share_max']} share cap can be enforced"
        )
    offered = parse_decimal(raw["offered_hours"])
    if offered < 0:
        raise EngineError(f"{pool_id}: offered_hours must be >= 0")
    cap = rules.dec("market", "day_ahead_share_max") * offered
    if supply > cap:
        return cap, True
    return supply, False


def _prepare_orders(
    orders_in: list,
    pools: dict[str, dict],
    rules: Ruleset,
    *,
    day_ahead: bool,
) -> tuple[list[Order], dict[str, dict]]:
    built: list[Order] = []
    pain_out: dict[str, dict] = {}
    seen: set[str] = set()
    ema = {pool_id: item["lagged_ema_cents"] for pool_id, item in pools.items()}
    hysteresis = rules.dec("market", "pain_hysteresis")
    for raw in orders_in:
        if not isinstance(raw, dict):
            raise EngineError("order must be an object")
        order_id = _required_str(raw, "order_id")
        if order_id in seen:
            raise EngineError(f"duplicate order_id {order_id}")
        seen.add(order_id)
        org_id = _required_str(raw, "org_id")
        hours = _required_decimal(raw, "hours")
        if hours <= 0:
            raise EngineError(f"{order_id}: hours must be positive")
        lottery = raw.get("lottery", 0)
        if isinstance(lottery, bool) or not isinstance(lottery, int) or lottery < 0:
            raise EngineError(f"{order_id}: lottery must be a non-negative integer")
        tip = raw.get("tip_cents", 0)
        tip_cents = _as_cents(tip, f"{order_id}: tip_cents")
        tier = raw.get("tier_rank", 0)
        if isinstance(tier, bool) or not isinstance(tier, int):
            raise EngineError(f"{order_id}: tier_rank must be an integer")
        rungs_raw = raw.get("rungs")
        if not isinstance(rungs_raw, list) or not rungs_raw:
            raise EngineError(f"{order_id}: rungs must be a non-empty list")
        rungs = tuple(_parse_rung(item, order_id) for item in rungs_raw)
        order = Order(
            order_id=order_id,
            org_id=org_id,
            rungs=rungs,
            hours=hours,
            tip_cents=tip_cents,
            tier_rank=tier,
            lottery=lottery,
            kind="day_ahead" if day_ahead else "realtime",
        )
        state = _parse_pain(raw.get("pain"), order_id)
        has_pain = raw.get("pain") is not None or any(
            rung.notify_cents is not None
            or rung.confirm_cents is not None
            or rung.auto_fallback_cents is not None
            or rung.stop_cents is not None
            for rung in rungs
        )
        if has_pain:
            cleared, state = apply_pain(order, ema, state, hysteresis=hysteresis)
        else:
            cleared = order
        pain_out[order_id] = {
            "pain": state.as_dict(),
            "dropped": cleared is None,
            "org_id": org_id,
            "hours": hours,
        }
        if cleared is not None:
            built.append(cleared)
    return built, pain_out


def _parse_rung(raw: object, order_id: str) -> Rung:
    if not isinstance(raw, dict):
        raise EngineError(f"{order_id}: rung must be an object")
    pool_id = _required_str(raw, "pool_id")
    max_cents = _required_cents(raw, "max_cents")
    willingness = raw.get("willingness_cents")
    if willingness is None:
        willingness = max_cents
    return Rung(
        pool_id=pool_id,
        max_cents=max_cents,
        willingness_cents=_as_cents(willingness, f"{order_id}: willingness_cents"),
        auto_fallback_cents=_optional_cents(raw.get("auto_fallback_cents"), order_id, "auto_fallback_cents"),
        confirm_cents=_optional_cents(raw.get("confirm_cents"), order_id, "confirm_cents"),
        stop_cents=_optional_cents(raw.get("stop_cents"), order_id, "stop_cents"),
        notify_cents=_optional_cents(raw.get("notify_cents"), order_id, "notify_cents"),
    )


def _parse_pain(raw: object, order_id: str) -> PainState:
    if raw is None:
        return PainState(fallen_back={})
    if not isinstance(raw, dict):
        raise EngineError(f"{order_id}: pain must be an object")
    fallen_raw = raw.get("fallen_back") or {}
    if not isinstance(fallen_raw, dict):
        raise EngineError(f"{order_id}: pain.fallen_back must be an object")
    fallen: dict[int, bool] = {}
    for key, value in fallen_raw.items():
        if not isinstance(value, bool):
            raise EngineError(f"{order_id}: pain flags must be boolean")
        fallen[int(key)] = value
    confirmed = raw.get("confirmed", False)
    if not isinstance(confirmed, bool):
        raise EngineError(f"{order_id}: pain.confirmed must be a boolean")
    notify_count = raw.get("notify_count", 0)
    if isinstance(notify_count, bool) or not isinstance(notify_count, int) or notify_count < 0:
        raise EngineError(f"{order_id}: pain.notify_count must be a non-negative integer")
    return PainState(fallen_back=fallen, confirmed=confirmed, notify_count=notify_count)


def _parse_boost(raw: object, rules: Ruleset, pool_id: str) -> BoostState:
    if raw is None:
        return BoostState(applied=rules.dec("boost", "b_max"), direction=0, prev_trail=None)
    if not isinstance(raw, dict):
        raise EngineError(f"{pool_id}: boost must be an object")
    applied = parse_decimal(raw.get("applied", rules.boost["b_max"]))
    if applied <= 0:
        raise EngineError(f"{pool_id}: boost.applied must be positive")
    direction = raw.get("direction", 0)
    if direction not in (-1, 0, 1):
        raise EngineError(f"{pool_id}: boost.direction must be -1, 0, or 1")
    prev = raw.get("prev_trail")
    prev_trail = None if prev is None else parse_decimal(prev)
    return BoostState(applied=applied, direction=int(direction), prev_trail=prev_trail)


def _rules(payload: dict, ruleset: Ruleset | None) -> Ruleset:
    if ruleset is not None:
        return ruleset
    version = payload.get("ruleset_version")
    try:
        if version not in (None, ""):
            return load_ruleset(str(version))
        return load_ruleset(active_version(_as_of(payload.get("round_start"))))
    except RulesetError as exc:
        raise EngineError(str(exc)) from exc


def _as_of(round_start: object) -> datetime:
    """When the caller omits ``ruleset_version``, select the ruleset in force.

    ``round_start`` makes that choice deterministic for the round. With no
    timestamp, the choice is the ruleset active at request time. An explicit
    version always wins, including one announced but not yet effective.
    """
    if round_start is None:
        return datetime.now(timezone.utc)
    if not isinstance(round_start, str) or not round_start:
        raise EngineError("round_start must be an ISO-8601 string")
    parsed = _parse_round_start(round_start)
    assert parsed is not None
    return parsed


def _degraded_flag(payload: dict) -> bool:
    """True when this round holds price, boost, and utilization memory.

    The HTTP API accepts the same flag on ``POST /v1/rounds/realtime``.
    ``degraded_round`` sets it for a local fallback. Day-ahead is refused.
    """
    if "degraded" not in payload or payload["degraded"] is None:
        return False
    if not isinstance(payload["degraded"], bool):
        raise EngineError("degraded must be a boolean")
    return payload["degraded"]


def _required_str(raw: dict, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise EngineError(f"{key} must be a non-empty string")
    return value


def _optional_decimal(raw: dict, key: str, default: object) -> Decimal:
    value = raw.get(key)
    if value is None:
        return parse_decimal(default) if not isinstance(default, Decimal) else default
    return parse_decimal(value)


def _required_decimal(raw: dict, key: str) -> Decimal:
    if key not in raw or raw[key] is None:
        raise EngineError(f"{key} is required")
    return parse_decimal(raw[key])


def _required_cents(raw: dict, key: str) -> int:
    if key not in raw or raw[key] is None:
        raise EngineError(f"{key} is required")
    return _as_cents(raw[key], key)


def _optional_cents(value: object, order_id: str, key: str) -> int | None:
    if value is None:
        return None
    return _as_cents(value, f"{order_id}: {key}")


def _as_cents(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EngineError(f"{label} must be an integer number of cents")
    if value < 0:
        raise EngineError(f"{label} must be >= 0")
    return value


def _parse_round_start(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise EngineError("round_start must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EngineError("round_start must be an ISO-8601 string") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

