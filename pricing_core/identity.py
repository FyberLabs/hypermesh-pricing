"""Canonical pool names.

A pool id is ``<class_id>@<region>``. While the market has no regions, the
region is ``default``. The engine accepts other ids so existing books keep
clearing; new callers should use this helper.
"""

from __future__ import annotations


def canonical_pool_id(class_id: str, region: str = "default") -> str:
    if not isinstance(class_id, str) or not class_id or "@" in class_id:
        raise ValueError("class_id must be a non-empty string without '@'")
    if not isinstance(region, str) or not region or "@" in region:
        raise ValueError("region must be a non-empty string without '@'")
    return f"{class_id}@{region}"
