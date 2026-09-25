"""Customer-facing copy for a published ruleset.

The sentences are built from the tuning values. A ruleset file stores the
same lines; ``validate_ruleset`` rejects a file whose stored lines differ
from these. Dashboard copy therefore cannot drift from the parameters.

The simulator source note, sha256, and clearing internals are not customer
copy. Lottery seeds and other organizations' orders are never inputs here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from pricing_core.money import parse_decimal

# Envelope fields on GET /v1/rulesets/{version}. False means Panopticon
# keeps the field for itself and does not render it on a dashboard.
ENVELOPE_CUSTOMER_VISIBLE = {
    "version": True,
    "effective_from": True,
    "changelog": True,
    "public_summary": True,
    "active": True,
    "sha256": False,
}

# Every machine parameter is classified. A new knob must be added here
# before a ruleset that contains it will load.
_ALWAYS_VISIBLE = frozenset(
    {
        "floor.depreciation_years",
        "floor.u_min",
        "floor.u_max",
        "floor.u_default",
        "boost.mode",
        "boost.b_max",
        "boost.min_hosts",
        "controller.delta",
        "controller.target_util",
        "controller.kappa",
        "controller.lambda",
        "controller.round_minutes",
        "market.tip_cap",
        "market.share_cap",
        "market.day_ahead_share_max",
        "market.day_ahead_upgrade",
    }
)
# Shown only while the conditional schedule is the rule customers are under.
_CONDITIONAL_VISIBLE = frozenset(
    {
        "boost.u_low",
        "boost.u_high",
        "boost.window_hours",
    }
)
_INTERNAL = frozenset(
    {
        "floor.hours_per_year",
        "floor.residual_default",
        "boost.hysteresis",
        "boost.smooth",
        "boost.thin_plateau",
        "market.max_passes",
        "market.pain_hysteresis",
        "market.smoothing_alpha",
    }
)


def customer_visible(path: str, raw: dict) -> bool:
    if path in _INTERNAL:
        return False
    if path in _CONDITIONAL_VISIBLE:
        return raw["boost"]["mode"] == "conditional"
    if path in _ALWAYS_VISIBLE:
        return True
    raise KeyError(path)


def known_parameter_paths() -> frozenset[str]:
    return _ALWAYS_VISIBLE | _CONDITIONAL_VISIBLE | _INTERNAL


def public_lines(raw: dict) -> list[str]:
    """Plain-English rules, one sentence per customer-visible behavior."""
    floor = raw["floor"]
    boost = raw["boost"]
    controller = raw["controller"]
    market = raw["market"]
    lines = [
        (
            f"Prices move at most {format_percent(controller['delta'])} "
            f"per {int(controller['round_minutes'])} minutes"
        ),
        f"Prices step toward the pool being {format_percent(controller['target_util'])} busy",
        f"Spike cap: {format_multiple(controller['kappa'])} the floor-based reserve",
        (
            f"When the recent day-ahead price is higher, the cap is "
            f"{format_multiple(controller['lambda'])} that price"
        ),
        f"Priority tip capped at {format_percent(market['tip_cap'])} of the live price",
    ]
    if boost["mode"] == "conditional":
        lines.append(
            f"New or quiet pools get up to a {format_multiple(boost['b_max'])} floor boost "
            f"that fades as the pool reaches {format_percent(boost['u_high'])} busy"
        )
        lines.append(
            f"The full boost holds while the pool is at or below {format_percent(boost['u_low'])} busy"
        )
        lines.append(
            f"The quiet-pool boost uses the last {int(boost['window_hours'])} hours of real-time use"
        )
    else:
        lines.append(f"The floor boost is fixed at {format_multiple(boost['b_max'])}")
    lines.append(f"No org may take more than {format_percent(market['share_cap'])} of a scarce pool")
    lines.append(
        f"At most {format_percent(market['day_ahead_share_max'])} of offered hours are sold a day ahead"
    )
    lines.append(f"A pool with fewer than {int(boost['min_hosts'])} hosts is priced at its reserve")
    lines.append(
        f"Hardware cost in the floor is spread over {int(floor['depreciation_years'])} years, "
        f"assuming the machine is busy between {format_percent(floor['u_min'])} "
        f"and {format_percent(floor['u_max'])} of the time"
    )
    lines.append(
        f"When utilization is not supplied, the floor assumes {format_percent(floor['u_default'])} busy"
    )
    if market["day_ahead_upgrade"]:
        lines.append("A day-ahead award can move up to a higher choice when the cleared price allows it")
    else:
        lines.append("A day-ahead award stays on the choice it won")
    return lines


def parameter_rows(raw: dict) -> list[dict]:
    rows: list[dict] = []
    for section in ("floor", "boost", "controller", "market"):
        for key, value in raw[section].items():
            path = f"{section}.{key}"
            rows.append(
                {
                    "path": path,
                    "value": value,
                    "customer_visible": customer_visible(path, raw),
                }
            )
    return rows


def ruleset_document(ruleset: object, *, active: bool) -> dict:
    """Machine parameters plus the public summary for one ruleset."""
    raw = ruleset.raw  # type: ignore[attr-defined]
    return {
        "engine_version": _engine_version(),
        "version": ruleset.version,  # type: ignore[attr-defined]
        "sha256": ruleset.sha256,  # type: ignore[attr-defined]
        "active": active,
        "effective_from": raw["effective_from"],
        "changelog": {
            "previous_version": raw["changelog"]["previous_version"],
            "summary": raw["changelog"]["summary"],
        },
        "public_summary": list(raw["public"]["lines"]),
        "customer_visible": dict(ENVELOPE_CUSTOMER_VISIBLE),
        "parameters": parameter_rows(raw),
    }


def choose_active(entries: list[tuple[str, datetime]], as_of: datetime) -> str:
    """Latest ruleset whose ``effective_from`` is at or before ``as_of``.

    ``entries`` is in publication order. The same timestamp keeps the
    later entry. A ruleset dated after ``as_of`` is announced, not active.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    chosen: str | None = None
    chosen_at: datetime | None = None
    for version, effective in entries:
        if effective.tzinfo is None:
            raise ValueError("effective_from must be timezone-aware")
        if effective <= as_of and (chosen_at is None or effective >= chosen_at):
            chosen = version
            chosen_at = effective
    if chosen is None:
        raise ValueError("no ruleset is effective yet")
    return chosen


def format_percent(value: object) -> str:
    number = parse_decimal(value) * Decimal(100)
    return f"{_plain(number)}%"


def format_multiple(value: object) -> str:
    return f"{_plain(parse_decimal(value))}x"


def _plain(number: Decimal) -> str:
    text = format(number.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _engine_version() -> str:
    from pricing_core import ENGINE_VERSION

    return ENGINE_VERSION


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
