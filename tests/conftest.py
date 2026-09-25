"""Put the market-sim snapshot on the import path for parity tests."""

from __future__ import annotations

import sys
from pathlib import Path

_REFERENCE = Path(__file__).resolve().parent / "reference"
if str(_REFERENCE) not in sys.path:
    sys.path.insert(0, str(_REFERENCE))
