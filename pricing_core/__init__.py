"""Hypermesh pricing core.

Pure clearing: floor, reserve, conditional boost, base-price controller,
orders, real-time deferred acceptance, and day-ahead uniform price.
Standard library only. Money is ``Decimal`` until an integer-cent boundary.
"""

ENGINE_VERSION = "0.1.1"
__version__ = ENGINE_VERSION

from pricing_core.boost import step_boost
from pricing_core.controller import corridor_cap_cents, step_base_cents
from pricing_core.engine import degraded_round, price_tokens
from pricing_core.floor import floor_dollars, reserve_cents
from pricing_core.identity import canonical_pool_id
from pricing_core.lottery import derive_lottery
from pricing_core.ruleset import load_ruleset

__all__ = [
    "ENGINE_VERSION",
    "canonical_pool_id",
    "corridor_cap_cents",
    "degraded_round",
    "derive_lottery",
    "floor_dollars",
    "load_ruleset",
    "price_tokens",
    "reserve_cents",
    "step_base_cents",
    "step_boost",
]
