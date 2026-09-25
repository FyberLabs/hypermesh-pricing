"""Commit-reveal lottery for tie-breaks.

The caller draws one secret ``round_seed`` per round, publishes
``sha256(round_seed)`` before clearing, and reveals the seed after.
Renters recompute ``derive_lottery(seed, order_id)`` and check that the
published commitment matches. The engine does not store the seed. A lower
integer wins a tie, then ``order_id``.
"""

from __future__ import annotations

import hashlib
import hmac


def lottery_commitment(round_seed: bytes) -> str:
    """Hex sha256 of the secret seed. Publish this before clearing."""
    if not isinstance(round_seed, (bytes, bytearray)) or len(round_seed) == 0:
        raise ValueError("round_seed must be non-empty bytes")
    return hashlib.sha256(bytes(round_seed)).hexdigest()


def derive_lottery(round_seed: bytes, order_id: str) -> int:
    """HMAC-SHA256(round_seed, order_id), first 8 bytes as a big-endian int."""
    if not isinstance(round_seed, (bytes, bytearray)) or len(round_seed) == 0:
        raise ValueError("round_seed must be non-empty bytes")
    if not isinstance(order_id, str) or not order_id:
        raise ValueError("order_id must be a non-empty string")
    digest = hmac.new(bytes(round_seed), order_id.encode("utf-8"), hashlib.sha256).digest()
    return int.from_bytes(digest[:8], "big")
