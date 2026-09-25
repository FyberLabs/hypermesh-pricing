"""Hourly floor and reserve.

Implements pricing-floor-and-bidding-2026-09-25.md §1–2.

    floor_hour   = energy_hour + depreciation_hour + host_margin_hour
    energy_hour  = P_loaded_kW × tariff_$per_kWh × overhead
    depreciation = (capex − residual) / (3 × 8760 × u)
    reserve_hour = floor_hour × floor_boost / (1 − take)

``u`` is network-set and clamped to the 30–70% band. Take is 0.08 on a
hardware lease and 0.12 on certified Full Model.

``floor_boost`` is a multiplier on the floor, applied before the take
gross-up. A static boost is constant. ``conditional_floor_boost`` holds
``b_max`` while trailing utilization is at or below ``u_low``, fades
linearly to 1.0 at ``u_high``, and stays at 1.0 above that. Infinite
thresholds reproduce the static boost. Depreciation is proportional to
``1/u`` and dominates the floor on the memo's datacenter classes, so a
boost ``b`` is roughly the same change as depreciating at utilization
``u/b`` (see ``equivalent_depreciation_utilization``). Energy and host
margin do not scale, so the match is approximate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

HOURS_PER_YEAR = 8760.0
DEPRECIATION_YEARS = 3.0
U_MIN = 0.30
U_MAX = 0.70
TAKE_HARDWARE_LEASE = 0.08
TAKE_CERTIFIED_FULL_MODEL = 0.12
STRIPE_RATE = 0.029
STRIPE_FIXED = 0.30


def clamp_utilization(u: float, lo: float = U_MIN, hi: float = U_MAX) -> float:
    """Clamp network utilization to the memo's 30–70% band."""
    if hi < lo:
        raise ValueError("utilization band is inverted")
    return min(hi, max(lo, float(u)))


def energy_per_hour(loaded_kw: float, tariff_per_kwh: float, overhead: float) -> float:
    if loaded_kw < 0 or tariff_per_kwh < 0 or overhead < 0:
        raise ValueError("energy inputs must be non-negative")
    return float(loaded_kw) * float(tariff_per_kwh) * float(overhead)


def depreciation_per_hour(
    capex: float,
    residual: float = 0.0,
    utilization: float = 0.50,
    *,
    years: float = DEPRECIATION_YEARS,
    hours_per_year: float = HOURS_PER_YEAR,
    clamp: bool = True,
) -> float:
    """Straight-line depreciation per paid hour.

    ``clamp=True`` applies the network 30–70% band before dividing. Pass
    ``clamp=False`` only to reproduce an unclamped sensitivity.
    """
    if years <= 0 or hours_per_year <= 0:
        raise ValueError("depreciation horizon must be positive")
    if capex < residual:
        raise ValueError("residual cannot exceed capex")
    u = clamp_utilization(utilization) if clamp else float(utilization)
    if u <= 0:
        raise ValueError("utilization must be positive")
    return (float(capex) - float(residual)) / (years * hours_per_year * u)


def floor_per_hour(
    loaded_kw: float,
    tariff_per_kwh: float,
    overhead: float,
    capex: float,
    residual: float = 0.0,
    utilization: float = 0.50,
    host_margin: float = 0.0,
    *,
    clamp_u: bool = True,
) -> float:
    if host_margin < 0:
        raise ValueError("host margin must be >= 0")
    energy = energy_per_hour(loaded_kw, tariff_per_kwh, overhead)
    depr = depreciation_per_hour(
        capex, residual, utilization, clamp=clamp_u
    )
    return energy + depr + float(host_margin)


def conditional_floor_boost(
    utilization: float,
    b_max: float,
    u_low: float,
    u_high: float,
    *,
    hysteresis: float = 0.0,
    direction: int = 0,
) -> float:
    """Boost that is ``b_max`` while a pool is thin and 1.0 once it is busy.

    ``utilization`` is a trailing utilization, not the round just cleared.
    The schedule is ``b_max`` at or below ``u_low``, 1.0 at or above
    ``u_high``, and linear in between. Infinite thresholds keep ``b_max``
    for every finite utilization: that is the static ``floor_boost``.

    ``direction`` is +1 when trailing utilization has been rising and −1
    when it has been falling. A positive ``hysteresis`` shifts both
    thresholds in that direction, so the boost does not reverse on a small
    wiggle around the boundary.
    """
    if b_max <= 0:
        raise ValueError("floor_boost must be positive")
    if hysteresis < 0:
        raise ValueError("boost hysteresis must be non-negative")
    if math.isinf(u_low) and math.isinf(u_high):
        return float(b_max)
    if u_high < u_low:
        raise ValueError("boost_u_high must be >= boost_u_low")
    shift = float(hysteresis) * int(direction)
    lo = float(u_low) + shift
    hi = float(u_high) + shift
    if hi <= lo:
        hi = lo + 1e-9
    u = float(utilization)
    if u <= lo:
        return float(b_max)
    if u >= hi:
        return 1.0
    if u_high == u_low and hysteresis == 0:
        return float(b_max) if u <= u_low else 1.0
    weight = (u - lo) / (hi - lo)
    return float(b_max) + (1.0 - float(b_max)) * weight


def reserve_per_hour(floor: float, take: float, boost: float = 1.0) -> float:
    """Gross-up so the host nets ``floor × boost`` after a fractional take.

    ``boost`` (``floor_boost``) multiplies the floor first. The reserve is
    ``floor × boost / (1 − take)``, not ``(floor / (1 − take)) × boost``
    applied as a second rounding step — those are equal for a scalar boost.
    """
    if floor < 0:
        raise ValueError("floor must be non-negative")
    if not 0 <= take < 1:
        raise ValueError("take must be in [0, 1)")
    if boost <= 0:
        raise ValueError("floor_boost must be positive")
    return float(floor) * float(boost) / (1.0 - float(take))


def equivalent_depreciation_utilization(utilization: float, boost: float) -> float:
    """Utilization ``u/b`` that matches scaling a depreciation-only floor by ``b``.

    Depreciation per paid hour is ``(capex − residual) / (3 × 8760 × u)``.
    Multiplying that term by ``b`` equals evaluating it at ``u/b``. The
    network band still clamps ``u`` itself; boost is how a scenario represents
    a cost that would have required utilization below 30% or simply a thicker
    margin. Energy does not follow the same ratio, so a full floor times ``b``
    is only approximately the floor at ``u/b``.
    """
    if utilization <= 0:
        raise ValueError("utilization must be positive")
    if boost <= 0:
        raise ValueError("floor_boost must be positive")
    return float(utilization) / float(boost)


def cash_checkout_minimum(
    hours: float,
    reserve_per_hour_usd: float,
    *,
    rate: float = STRIPE_RATE,
    fixed: float = STRIPE_FIXED,
) -> float:
    """Card checkout total that still nets ``hours × reserve`` after Stripe.

    The worked reserve column in the pricing memo leaves this term out because
    it depends on lease length. USDC renters pay the listed price and skip it.
    """
    if hours < 0:
        raise ValueError("hours must be non-negative")
    if not 0 <= rate < 1:
        raise ValueError("stripe rate must be in [0, 1)")
    if fixed < 0:
        raise ValueError("stripe fixed fee must be non-negative")
    return (hours * float(reserve_per_hour_usd) + float(fixed)) / (1.0 - float(rate))


@dataclass(frozen=True)
class HardwareClass:
    """Class-level planning inputs. Not a measured host."""

    class_id: str
    label: str
    capex: float
    loaded_kw: float
    tariff_per_kwh: float
    overhead: float
    site_type: str
    utilization_u: float = 0.50
    residual: float = 0.0
    host_margin: float = 0.0
    # None means "use the scenario's global floor_boost" / band.
    floor_boost: float | None = None
    boost_u_low: float | None = None
    boost_u_high: float | None = None

    def floor(self, *, clamp_u: bool = True) -> float:
        return floor_per_hour(
            self.loaded_kw,
            self.tariff_per_kwh,
            self.overhead,
            self.capex,
            self.residual,
            self.utilization_u,
            self.host_margin,
            clamp_u=clamp_u,
        )

    def reserve(self, take: float, *, boost: float = 1.0, clamp_u: bool = True) -> float:
        applied = float(self.floor_boost) if self.floor_boost is not None else float(boost)
        return reserve_per_hour(self.floor(clamp_u=clamp_u), take, applied)


def hardware_class_from_dict(raw: dict) -> HardwareClass:
    return HardwareClass(
        class_id=str(raw["id"]),
        label=str(raw.get("label", raw["id"])),
        capex=float(raw["capex"]),
        loaded_kw=float(raw["loaded_kw"]),
        tariff_per_kwh=float(raw["tariff_per_kwh"]),
        overhead=float(raw["overhead"]),
        site_type=str(raw.get("site_type", "unspecified")),
        utilization_u=float(raw.get("utilization_u", 0.50)),
        residual=float(raw.get("residual", 0.0)),
        host_margin=float(raw.get("host_margin", 0.0)),
        floor_boost=(None if raw.get("floor_boost") is None else float(raw["floor_boost"])),
        boost_u_low=(None if raw.get("boost_u_low") is None else float(raw["boost_u_low"])),
        boost_u_high=(None if raw.get("boost_u_high") is None else float(raw["boost_u_high"])),
    )
