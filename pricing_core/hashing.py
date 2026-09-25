"""Canonical request hash.

The same logical input always hashes the same, including key order.
``idempotency_key`` is not part of the hash: it is an HTTP echo, and the
price depends only on the body. The service stores nothing, so a retry of
the same body is safe.
"""

from __future__ import annotations

import hashlib
import json


def request_hash(payload: dict) -> str:
    body = {key: value for key, value in payload.items() if key != "idempotency_key"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
