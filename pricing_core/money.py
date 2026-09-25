"""Decimal money helpers.

Public amounts are integer cents. Internal arithmetic uses ``Decimal`` and
rejects ``float`` so a binary fraction cannot change a cleared price.
Rounding a reserve or a payment never lands below the exact reserve.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN

CENT = Decimal("0.01")
ONE = Decimal(1)
HUNDRED = Decimal(100)
Q8 = Decimal("0.00000001")


def D(value: Decimal | int | str) -> Decimal:
    """Parse a decimal without accepting a binary float."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("decimal values must be Decimal, int, or str")
    try:
        return Decimal(value)
    except Exception as exc:  # noqa: BLE001 — Decimal raises several subclasses
        raise ValueError(f"invalid decimal {value!r}") from exc


def parse_decimal(value: object) -> Decimal:
    """Accept a JSON string or number.

    JSON numbers arrive as ``float`` or ``int``. ``str(float)`` is the
    shortest round-trip, which is exact for the short decimals in the
    ruleset (0.35, 0.70, 1.3). Callers that need a non-terminating value
    should send a string.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("boolean is not a decimal")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        return D(value)
    raise TypeError(f"cannot parse decimal from {type(value).__name__}")


def cents_to_dollars(cents: int) -> Decimal:
    if not isinstance(cents, int) or isinstance(cents, bool):
        raise TypeError("cents must be an int")
    return Decimal(cents) / HUNDRED


def ceil_cents(amount: Decimal) -> int:
    """Smallest integer cent that is not below ``amount`` dollars."""
    if amount < 0:
        raise ValueError("money amount must be non-negative")
    return int((amount * HUNDRED).to_integral_value(rounding=ROUND_CEILING))


def half_even_cents(amount: Decimal) -> int:
    if amount < 0:
        raise ValueError("money amount must be non-negative")
    return int((amount * HUNDRED).to_integral_value(rounding=ROUND_HALF_EVEN))


def floor_div_cents(numerator_cents: Decimal) -> int:
    """Integer cents, rounded toward zero. Used for the tip cap."""
    if numerator_cents < 0:
        raise ValueError("money amount must be non-negative")
    return int(numerator_cents.to_integral_value(rounding=ROUND_FLOOR))


def at_least_reserve(cents: int, reserve_cents: int) -> int:
    """A quoted cent price is never below the reserve."""
    if cents < reserve_cents:
        return reserve_cents
    return cents


def dec_str(value: Decimal, places: str = "0.00000001") -> str:
    """Stable, non-scientific decimal string."""
    quant = value.quantize(Decimal(places), rounding=ROUND_HALF_EVEN)
    text = format(quant, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def scale_cents(rate_cents: int, hours: Decimal) -> int:
    """``rate_cents × hours``, half-even to an integer cent.

    A whole number of hours is exact. ``pay_cents`` and ``tip_cents`` are
    per box-hour; this is how a fill becomes a total.
    """
    if isinstance(rate_cents, bool) or not isinstance(rate_cents, int):
        raise TypeError("rate_cents must be an int")
    if rate_cents < 0 or hours < 0:
        raise ValueError("rate and hours must be non-negative")
    return int((Decimal(rate_cents) * hours).to_integral_value(rounding=ROUND_HALF_EVEN))


def clamp_int(value: int, lo: int, hi: int) -> int:
    if hi < lo:
        return lo
    return min(max(value, lo), hi)
