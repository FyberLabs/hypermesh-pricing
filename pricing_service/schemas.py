"""Request and response models for the pricing HTTP API.

Decimal inputs are strings so a JSON number cannot become a binary float
before the core parses it. Money at the boundary is integer cents.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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


class PoolIn(_Model):
    pool_id: str
    class_id: str
    region: str
    supply_hours: str
    n_hosts: int = Field(ge=0)
    reserve_cents: int = Field(ge=0)
    prev_base_cents: int = Field(ge=0)
    trailing_util_24h: str
    day_ahead_median_7d_cents: int | None = Field(default=None, ge=0)
    offered_hours: str | None = None
    floor_cents: int | None = Field(default=None, ge=0)
    floor_usd: str | None = None
    take: str | None = None
    ema_cents: int | None = Field(default=None, ge=0)
    boost: BoostIn | None = None


class RoundIn(_Model):
    round_id: str
    round_start: str | None = None
    ruleset_version: str | None = None
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


class FillOut(_Model):
    order_id: str
    org_id: str
    pool_id: str
    rung_index: int
    hours: str
    pay_cents: int
    tip_cents: int


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
    rationed: bool
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
    pools: list[PoolOut]
    fills: list[FillOut]
    unfilled: list[UnfilledOut]
    pain_by_order: dict[str, PainIn]
    upgrade_applied: bool | None = None
    upgrade_gains: int | None = None


class FloorOut(_Model):
    engine_version: str
    ruleset_version: str
    floor_cents: int
    reserve_cents: int
    floor_usd: str
    reserve_usd: str
    u_used: str
    boost_used: str


class RulesetEntry(_Model):
    version: str
    sha256: str
    source: str


class RulesetListOut(_Model):
    engine_version: str
    default: str
    rulesets: list[RulesetEntry]


class HealthOut(_Model):
    status: str
    engine_version: str
