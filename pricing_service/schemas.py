"""Request and response models for the pricing HTTP API.

Decimal inputs are strings so a JSON number cannot become a binary float
before the core parses it. Money at the boundary is integer cents.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BoostIn(_Model):
    applied: str = "1.3"
    direction: Literal[-1, 0, 1] = 0
    prev_trail: str | None = None


class BoostOut(_Model):
    applied: str
    direction: int
    prev_trail: str | None = None


class RungIn(_Model):
    pool_id: str
    max_cents: int = Field(ge=0)
    willingness_cents: int | None = Field(default=None, ge=0)
    notify_cents: int | None = Field(default=None, ge=0)
    confirm_cents: int | None = Field(default=None, ge=0)
    auto_fallback_cents: int | None = Field(default=None, ge=0)
    stop_cents: int | None = Field(default=None, ge=0)


class PainIn(_Model):
    fallen_back: dict[str, bool] = Field(default_factory=dict)
    confirmed: bool = False
    notify_count: int = Field(default=0, ge=0)


class OrderIn(_Model):
    order_id: str
    org_id: str
    lottery: int = Field(default=0, ge=0)
    tip_cents: int = Field(default=0, ge=0)
    hours: str
    tier_rank: int = 0
    rungs: list[RungIn] = Field(min_length=1)
    pain: PainIn | None = None


class SupplyTierIn(_Model):
    reserve_cents: int = Field(ge=0)
    hours: str


class PoolIn(_Model):
    pool_id: str
    class_id: str
    region: str
    supply_hours: str | None = None
    n_hosts: int = Field(ge=0)
    reserve_cents: int | None = Field(default=None, ge=0)
    supply_tiers: list[SupplyTierIn] | None = Field(default=None, min_length=1)
    prev_base_cents: int = Field(ge=0)
    trailing_util_24h: str
    day_ahead_median_7d_cents: int | None = Field(default=None, ge=0)
    offered_hours: str | None = None
    floor_cents: int | None = Field(default=None, ge=0)
    floor_usd: str | None = None
    take: str | None = None
    processor_pct: str | None = None
    processor_fixed_cents: int | None = Field(default=None, ge=0)
    expected_hours_per_txn: str | None = None
    ema_cents: int | None = Field(default=None, ge=0)
    boost: BoostIn | None = None

    @model_validator(mode="after")
    def one_supply_form(self) -> "PoolIn":
        if self.supply_tiers:
            return self
        if not self.supply_hours or self.reserve_cents is None:
            raise ValueError("supply_hours and reserve_cents are required when supply_tiers is omitted")
        return self


class RoundIn(_Model):
    round_id: str
    round_start: str | None = None
    ruleset_version: str | None = None
    degraded: bool = Field(
        default=False,
        description="Hold the base and freeze boost and utilization memory. Day-ahead is refused.",
    )
    processor_pct: str | None = None
    processor_fixed_cents: int | None = Field(default=None, ge=0)
    expected_hours_per_txn: str | None = None
    pools: list[PoolIn] = Field(min_length=1)
    orders: list[OrderIn]


class FloorIn(_Model):
    ruleset_version: str | None = None
    loaded_kw: str
    tariff_per_kwh: str
    overhead: str
    capex: str
    residual: str | None = None
    u: str | None = None
    host_margin: str | None = None
    take: str
    boost: str
    processor_pct: str | None = None
    processor_fixed_cents: int | None = Field(default=None, ge=0)
    expected_hours_per_txn: str | None = None


class TokenPriceIn(_Model):
    ruleset_version: str | None = None
    pool_id: str | None = None
    class_id: str | None = None
    model_id: str | None = None
    base_cents: int = Field(ge=0)
    reserve_cents: int = Field(ge=0)
    take: str = Field(description="P2's own take for this model. The engine has no default fee.")
    input_tokens_per_second: str
    output_tokens_per_second: str
    u_batch: str = "1"


class FillOut(_Model):
    order_id: str
    org_id: str
    pool_id: str
    rung_index: int
    hours: str
    pay_cents: int = Field(description="Per box-hour clearing price. Does not include the tip.")
    tip_cents: int = Field(
        description="Per box-hour priority tip actually charged. Zero unless the pool is scarce."
    )
    tip_total_cents: int = Field(description="tip_cents × hours, half-even to a cent.")
    total_cents: int = Field(description="pay_cents × hours + tip_total_cents.")
    price_lock_hours: str | None = Field(
        default=None,
        description="Real-time only. Hours this fill's clearing price stays locked, at most lock_hours_max.",
    )


class UnfilledOut(_Model):
    order_id: str
    org_id: str
    hours: str


class PoolOut(_Model):
    pool_id: str
    class_id: str
    region: str
    base_cents: int
    next_base_cents: int
    reserve_cents: int
    cap_cents: int
    utilization: str
    scarce: bool
    org_cap_applied: bool = Field(description="The 25% scarce-pool org cap bound at least one org.")
    rationed: bool
    boost_active: bool = Field(description="The floor boost multiple in force is above 1.")
    boost_multiple: str = Field(description="Current floor-boost multiple. Safe to show customers.")
    at_cap: bool = Field(description="The live price is at or above the spike cap.")
    at_floor: bool = Field(description="The live price is at the floor-based reserve, the minimum price.")
    degraded: bool = Field(description="True when this round held the base and froze boost and utilization memory.")
    ruleset_version: str = Field(description="Ruleset that priced this pool. Stamp it on the receipt.")
    demand_hours: str
    supply_hours: str
    supply_clamped: bool
    reserve_source: str
    boost: BoostOut
    next_ema_cents: int


class RoundOut(_Model):
    engine_version: str
    ruleset_version: str
    round_id: str
    round_start: str | None = None
    passes: int
    hit_iteration_cap: bool
    price_lock_hours_max: int = Field(description="Ruleset cap on a real-time price lock, in hours.")
    pools: list[PoolOut]
    fills: list[FillOut]
    unfilled: list[UnfilledOut]
    pain_by_order: dict[str, PainIn]
    upgrade_applied: bool | None = None
    upgrade_gains: int | None = None
    request_hash: str
    idempotency_key: str | None = None


class FloorOut(_Model):
    engine_version: str
    ruleset_version: str
    floor_cents: int
    reserve_cents: int
    floor_usd: str
    reserve_usd: str
    u_used: str
    boost_used: str
    processor_pct: str
    processor_fixed_cents: int
    expected_hours_per_txn: str | None = None
    request_hash: str
    idempotency_key: str | None = None


class TokenPriceOut(_Model):
    engine_version: str
    ruleset_version: str
    pool_id: str | None = None
    class_id: str | None = None
    model_id: str | None = None
    take: str
    base_cents: int
    reserve_cents: int
    reference_cents: int = Field(description="max(reserve, base), before the take gross-up.")
    billed_hour_cents: int = Field(description="Ceiling of reference / (1 − take). The hour a renter is billed from.")
    input_per_1k_cents: int
    output_per_1k_cents: int
    loaded_hour_min_cents: int = Field(description="The pool reserve. A host hour is not priced below this.")
    u_batch: str
    request_hash: str
    idempotency_key: str | None = None


class RulesetEntry(_Model):
    version: str
    effective_from: str


class RulesetListOut(_Model):
    engine_version: str
    default: str
    rulesets: list[RulesetEntry]


class ChangelogOut(_Model):
    previous_version: str | None
    summary: str


class ParameterOut(_Model):
    path: str
    value: str | int | bool
    customer_visible: bool = Field(
        description="True when a renter or host dashboard may render this parameter."
    )


class RulesetDetailOut(_Model):
    engine_version: str
    version: str
    active: bool
    effective_from: str
    changelog: ChangelogOut
    public_summary: list[str]
    customer_visible: dict[str, bool]
    parameters: list[ParameterOut]


class HealthOut(_Model):
    status: str
    engine_version: str
