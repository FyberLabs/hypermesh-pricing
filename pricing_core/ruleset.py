"""Load and check versioned ruleset files.

A published ruleset file is immutable. A tuning change is a new version
filename. Parsing uses the standard-library JSON decoder so the core stays
free of YAML or schema libraries. Every tuning value lives in the file;
the platform fee (take) is intentionally absent.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from pricing_core.money import D, parse_decimal
from pricing_core.transparency import choose_active, customer_visible, known_parameter_paths, public_lines

# Bump only by shipping a new file. Tests pin the bytes of each published name.
PUBLISHED_VERSIONS = ("2026-09-25.1",)


class RulesetError(ValueError):
    """The ruleset file is missing, malformed, or fails the schema check."""


@dataclass(frozen=True)
class Ruleset:
    version: str
    sha256: str
    source: str
    raw: dict

    @property
    def floor(self) -> dict:
        return self.raw["floor"]

    @property
    def boost(self) -> dict:
        return self.raw["boost"]

    @property
    def controller(self) -> dict:
        return self.raw["controller"]

    @property
    def market(self) -> dict:
        return self.raw["market"]

    def dec(self, section: str, key: str) -> Decimal:
        return D(self.raw[section][key])


def ruleset_dir() -> Path:
    override = os.environ.get("PRICING_RULESET_DIR")
    if override:
        path = Path(override)
        if not path.is_dir():
            raise RulesetError(f"PRICING_RULESET_DIR is not a directory: {path}")
        return path
    candidates = [
        Path.cwd() / "rulesets",
        Path(__file__).resolve().parents[1] / "rulesets",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise RulesetError("rulesets directory not found")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def active_version(as_of: datetime, *, root: Path | None = None) -> str:
    """Published ruleset in force at ``as_of``.

    A file whose ``effective_from`` is later than ``as_of`` is published
    for announcement and is not selected. Callers that name a version
    explicitly still get that version.
    """
    if as_of.tzinfo is None:
        raise RulesetError("as_of must be timezone-aware")
    directory = root or ruleset_dir()
    entries: list[tuple[str, datetime]] = []
    for version in PUBLISHED_VERSIONS:
        path = directory / f"{version}.json"
        if not path.is_file():
            continue
        loaded = load_ruleset(version, root=directory)
        entries.append((version, parse_effective_from(loaded.raw["effective_from"])))
    try:
        return choose_active(entries, as_of)
    except ValueError as exc:
        raise RulesetError(str(exc)) from exc


def load_ruleset(version: str | None = None, *, root: Path | None = None) -> Ruleset:
    """Load one published ruleset.

    ``None`` selects the ruleset active at the current UTC time, which
    skips a version that has been published but is not yet effective.
    """
    if version is None:
        version = active_version(datetime.now(timezone.utc), root=root)
    chosen = version
    if chosen not in PUBLISHED_VERSIONS:
        raise RulesetError(f"unknown ruleset version {chosen!r}")
    directory = root or ruleset_dir()
    path = directory / f"{chosen}.json"
    if not path.is_file():
        raise RulesetError(f"ruleset file not found: {path.name}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RulesetError(f"{path.name} is not valid JSON") from exc
    validate_ruleset(raw)
    if raw["version"] != chosen:
        raise RulesetError(
            f"{path.name} declares version {raw['version']!r}, expected {chosen!r}"
        )
    return Ruleset(
        version=chosen,
        sha256=file_sha256(path),
        source=str(raw["source"]),
        raw=raw,
    )


def list_rulesets(*, root: Path | None = None) -> list[Ruleset]:
    directory = root or ruleset_dir()
    found: list[Ruleset] = []
    for version in PUBLISHED_VERSIONS:
        path = directory / f"{version}.json"
        if path.is_file():
            found.append(load_ruleset(version, root=directory))
    return found


def validate_ruleset(raw: object) -> None:
    """Schema check. Raises ``RulesetError`` on the first problem."""
    if not isinstance(raw, dict):
        raise RulesetError("ruleset must be a JSON object")
    if "take" in raw or "take" in raw.get("market", {}) or "take" in raw.get("floor", {}):
        raise RulesetError("ruleset must not bake in a platform fee; pass take per call")
    _require_keys(
        raw,
        [
            "version",
            "source",
            "effective_from",
            "changelog",
            "public",
            "floor",
            "boost",
            "controller",
            "market",
        ],
    )
    if not isinstance(raw["version"], str) or not raw["version"]:
        raise RulesetError("version must be a non-empty string")
    source = raw["source"]
    if not isinstance(source, str) or "Simulated" not in source:
        raise RulesetError("source must label tuning values as Simulated")
    floor = _section(raw, "floor")
    _require_keys(
        floor,
        ["depreciation_years", "hours_per_year", "u_min", "u_max", "u_default", "residual_default"],
    )
    years = _positive_int(floor["depreciation_years"], "floor.depreciation_years")
    hours = _positive_int(floor["hours_per_year"], "floor.hours_per_year")
    if years != 3 or hours != 8760:
        raise RulesetError("depreciation horizon must be 3 years of 8760 hours")
    u_min = _unit_interval(floor["u_min"], "floor.u_min", positive=True)
    u_max = _unit_interval(floor["u_max"], "floor.u_max", positive=True)
    u_default = _unit_interval(floor["u_default"], "floor.u_default", positive=True)
    if not (u_min <= u_default <= u_max):
        raise RulesetError("floor utilization band must contain u_default")
    residual = parse_decimal(floor["residual_default"])
    if residual < 0:
        raise RulesetError("residual_default must be >= 0")

    boost = _section(raw, "boost")
    _require_keys(
        boost,
        ["mode", "b_max", "u_low", "u_high", "hysteresis", "smooth", "window_hours", "min_hosts", "thin_plateau"],
    )
    if boost["mode"] not in {"conditional", "static"}:
        raise RulesetError("boost.mode must be conditional or static")
    b_max = parse_decimal(boost["b_max"])
    if b_max <= 0:
        raise RulesetError("boost.b_max must be positive")
    if boost["mode"] == "conditional" and b_max < 1:
        raise RulesetError("conditional b_max must be >= 1")
    u_low = parse_decimal(boost["u_low"])
    u_high = parse_decimal(boost["u_high"])
    if u_low < 0 or u_high < u_low:
        raise RulesetError("boost band must satisfy 0 <= u_low <= u_high")
    hysteresis = parse_decimal(boost["hysteresis"])
    if hysteresis < 0:
        raise RulesetError("boost.hysteresis must be >= 0")
    smooth = parse_decimal(boost["smooth"])
    if not (Decimal(0) < smooth <= 1):
        raise RulesetError("boost.smooth must be in (0, 1]")
    _positive_int(boost["window_hours"], "boost.window_hours")
    _positive_int(boost["min_hosts"], "boost.min_hosts")
    if not isinstance(boost["thin_plateau"], bool):
        raise RulesetError("boost.thin_plateau must be a boolean")

    controller = _section(raw, "controller")
    _require_keys(controller, ["delta", "target_util", "kappa", "lambda", "round_minutes"])
    delta = parse_decimal(controller["delta"])
    if delta < 0:
        raise RulesetError("controller.delta must be >= 0")
    target = parse_decimal(controller["target_util"])
    if not (Decimal(0) < target < 1):
        raise RulesetError("controller.target_util must be in (0, 1)")
    kappa = parse_decimal(controller["kappa"])
    shock = parse_decimal(controller["lambda"])
    if kappa < 1 or shock < 0:
        raise RulesetError("controller kappa must be >= 1 and lambda >= 0")
    minutes = _positive_int(controller["round_minutes"], "controller.round_minutes")
    if 60 % minutes != 0:
        raise RulesetError("controller.round_minutes must divide 60")

    market = _section(raw, "market")
    _require_keys(
        market,
        [
            "tip_cap",
            "share_cap",
            "day_ahead_share_max",
            "max_passes",
            "pain_hysteresis",
            "smoothing_alpha",
            "day_ahead_upgrade",
        ],
    )
    tip = parse_decimal(market["tip_cap"])
    if tip < 0:
        raise RulesetError("market.tip_cap must be >= 0")
    share = parse_decimal(market["share_cap"])
    if not (Decimal(0) < share <= 1):
        raise RulesetError("market.share_cap must be in (0, 1]")
    day_ahead = parse_decimal(market["day_ahead_share_max"])
    if not (Decimal(0) < day_ahead < 1):
        raise RulesetError("market.day_ahead_share_max must be in (0, 1)")
    _positive_int(market["max_passes"], "market.max_passes")
    pain = parse_decimal(market["pain_hysteresis"])
    if pain < 0:
        raise RulesetError("market.pain_hysteresis must be >= 0")
    alpha = parse_decimal(market["smoothing_alpha"])
    if not (Decimal(0) < alpha <= 1):
        raise RulesetError("market.smoothing_alpha must be in (0, 1]")
    if not isinstance(market["day_ahead_upgrade"], bool):
        raise RulesetError("market.day_ahead_upgrade must be a boolean")
    _check_transparency(raw)


def parse_effective_from(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise RulesetError("effective_from must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RulesetError("effective_from must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise RulesetError("effective_from must include a timezone")
    return parsed.astimezone(timezone.utc)


def _check_transparency(raw: dict) -> None:
    parse_effective_from(raw["effective_from"])
    changelog = raw["changelog"]
    if not isinstance(changelog, dict):
        raise RulesetError("changelog must be an object")
    _require_keys(changelog, ["previous_version", "summary"])
    previous = changelog["previous_version"]
    if previous is not None and (
        not isinstance(previous, str) or not previous or previous == raw["version"]
    ):
        raise RulesetError("changelog.previous_version must be null or a different version")
    summary = changelog["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise RulesetError("changelog.summary must be a non-empty string")
    public = raw["public"]
    if not isinstance(public, dict):
        raise RulesetError("public must be an object")
    lines = public.get("lines")
    if not isinstance(lines, list) or not lines or any(not isinstance(line, str) or not line.strip() for line in lines):
        raise RulesetError("public.lines must be a non-empty list of strings")
    expected = public_lines(raw)
    if list(lines) != expected:
        raise RulesetError("public lines do not match the parameters")
    for section in ("floor", "boost", "controller", "market"):
        for key in raw[section]:
            path = f"{section}.{key}"
            if path not in known_parameter_paths():
                raise RulesetError(f"unclassified parameter {path}")
            customer_visible(path, raw)


def _section(raw: dict, key: str) -> dict:
    value = raw[key]
    if not isinstance(value, dict):
        raise RulesetError(f"{key} must be an object")
    return value


def _require_keys(obj: dict, keys: list[str]) -> None:
    missing = [key for key in keys if key not in obj]
    if missing:
        raise RulesetError(f"missing keys: {', '.join(missing)}")


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RulesetError(f"{label} must be a positive integer")
    return value


def _unit_interval(value: object, label: str, *, positive: bool) -> Decimal:
    number = parse_decimal(value)
    if positive:
        if not (Decimal(0) < number <= 1):
            raise RulesetError(f"{label} must be in (0, 1]")
    elif not (Decimal(0) <= number <= 1):
        raise RulesetError(f"{label} must be in [0, 1]")
    return number
