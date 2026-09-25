"""Published rulesets are schema-checked and immutable."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pricing_core.ruleset import RulesetError, load_ruleset, ruleset_dir, validate_ruleset

# Frozen bytes of rulesets/2026-09-25.1.json. A tuning change is a new version
# file, not an edit of this one.
PINNED_SHA256 = "521295aad5ba0891b57a09c6c43572c43e938c7ea5d34674c4fc598b22158066"
ROOT = Path(__file__).resolve().parents[1]


def test_published_ruleset_bytes_are_frozen():
    path = ruleset_dir() / "2026-09-25.1.json"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sidecar = (path.parent / f"{path.name}.sha256").read_text(encoding="utf-8").strip()
    assert digest == PINNED_SHA256
    assert sidecar == PINNED_SHA256
    loaded = load_ruleset("2026-09-25.1")
    assert loaded.sha256 == PINNED_SHA256
    assert loaded.version == "2026-09-25.1"


def test_published_ruleset_has_no_platform_fee_and_is_labeled_simulated():
    raw = json.loads((ruleset_dir() / "2026-09-25.1.json").read_text(encoding="utf-8"))
    blob = json.dumps(raw)
    assert "take" not in raw
    assert "take" not in raw["market"]
    assert "take" not in raw["floor"]
    assert "Simulated" in raw["source"]
    assert "0.08" not in blob
    assert "0.12" not in blob
    assert raw["controller"]["delta"] == "0.025"
    assert raw["controller"]["kappa"] == "3"
    assert raw["controller"]["lambda"] == "1.5"
    assert raw["boost"]["b_max"] == "1.3"
    assert raw["boost"]["u_low"] == "0.35"
    assert raw["boost"]["u_high"] == "0.55"
    assert raw["market"]["day_ahead_share_max"] == "0.75"
    assert raw["market"]["tip_cap"] == "0.10"
    assert raw["market"]["share_cap"] == "0.25"


def test_schema_file_matches_the_checker():
    schema = json.loads((ROOT / "rulesets" / "schema.json").read_text(encoding="utf-8"))
    raw = json.loads((ROOT / "rulesets" / "2026-09-25.1.json").read_text(encoding="utf-8"))
    validate_ruleset(raw)
    assert set(schema["required"]) <= set(raw)
    for section in ("floor", "boost", "controller", "market"):
        assert set(schema["properties"][section]["required"]) <= set(raw[section])


def test_schema_rejects_a_baked_in_take_and_a_missing_knob():
    raw = json.loads((ROOT / "rulesets" / "2026-09-25.1.json").read_text(encoding="utf-8"))
    baked = json.loads(json.dumps(raw))
    baked["market"]["take"] = "0.08"
    with pytest.raises(RulesetError, match="platform fee"):
        validate_ruleset(baked)
    missing = json.loads(json.dumps(raw))
    del missing["controller"]["lambda"]
    with pytest.raises(RulesetError, match="lambda"):
        validate_ruleset(missing)


def test_unknown_ruleset_version_is_rejected(tmp_path: Path):
    with pytest.raises(RulesetError):
        load_ruleset("does-not-exist", root=tmp_path)
