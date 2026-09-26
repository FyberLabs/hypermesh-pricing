"""Process entrypoint for the pricing HTTP API.

The container runs ``python -m pricing_service``. That exits before the
socket opens when ``PRICING_SERVICE_TOKEN`` is unset or blank. The value
is never printed.
"""

from __future__ import annotations

import os
import sys

TOKEN_ENV = "PRICING_SERVICE_TOKEN"
HOST = "0.0.0.0"
PORT = 8080


def require_token() -> None:
    token = os.environ.get(TOKEN_ENV)
    if token is None or token.strip() == "":
        print(f"{TOKEN_ENV} is required", file=sys.stderr)
        raise SystemExit(1)


def main() -> None:
    require_token()
    import uvicorn

    uvicorn.run("pricing_service.app:app", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
