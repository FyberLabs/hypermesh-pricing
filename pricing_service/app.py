"""Stateless pricing HTTP API.

Auth is a bearer token from ``PRICING_SERVICE_TOKEN``. ``python -m
pricing_service`` (the container entrypoint) exits before listening when
that variable is unset or blank. Protected ``/v1`` calls also fail closed
if a process is serving without it. ``GET /healthz`` and the read-only
ruleset routes stay open. Those ruleset responses contain only
customer-safe fields. Panopticon should still proxy them through its
public edge. The token is never logged and is not a configuration file.

An optional ``Idempotency-Key`` header is echoed and is not part of
``request_hash``. The process stores nothing. The same body always prices
the same, so a retry is safe.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pricing_core import ENGINE_VERSION
from pricing_core.engine import (
    EngineError,
    price_day_ahead,
    price_floor,
    price_realtime,
    price_tokens,
)
from pricing_core.ruleset import RulesetError, active_version, list_rulesets, load_ruleset
from pricing_core.transparency import customer_ruleset_document, utc_now
from pricing_service.schemas import (
    FloorIn,
    FloorOut,
    HealthOut,
    RoundIn,
    RoundOut,
    RulesetDetailOut,
    RulesetListOut,
    TokenPriceIn,
    TokenPriceOut,
)

TOKEN_ENV = "PRICING_SERVICE_TOKEN"
_bearer = HTTPBearer(
    auto_error=False,
    description="Internal service token from the PRICING_SERVICE_TOKEN environment variable.",
)


def _documented_bearer(
    _: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """The middleware enforces the token. This dependency only publishes it."""
    return None


def _public_path(path: str, method: str) -> bool:
    if path in {"/healthz", "/openapi.json", "/docs", "/redoc"}:
        return True
    if method != "GET":
        return False
    if path in {"/v1/rulesets", "/v1/rulesets/active"}:
        return True
    prefix = "/v1/rulesets/"
    if path.startswith(prefix):
        rest = path[len(prefix):]
        return bool(rest) and "/" not in rest
    return False


def create_app() -> FastAPI:
    app = FastAPI(
        title="Hypermesh pricing",
        version=ENGINE_VERSION,
        description=(
            "Stateless pricing engine. Tuning values in the published "
            "ruleset are simulated defaults from the private market-sim reference, "
            "not measured prices. "
            "The platform fee is a per-request take; the ruleset does not set one. "
            "GET /v1/rulesets/active is the customer-facing rules card: show "
            "public_summary and the parameters returned there. Those GET routes "
            "are unauthenticated and return customer-safe fields only. "
            "Panopticon should still proxy them through its public edge. "
            "A canonical pool id is <class_id>@<region>, with region default "
            "while there are no regions. "
            "Pin the container by digest: "
            "ghcr.io/fyberlabs/hypermesh-pricing@sha256:<digest>."
        ),
        openapi_url="/openapi.json",
    )

    @app.middleware("http")
    async def service_token(request: Request, call_next):
        path = request.url.path.rstrip("/") or "/"
        if _public_path(path, request.method):
            return await call_next(request)
        if not path.startswith("/v1/"):
            return await call_next(request)
        expected = os.environ.get(TOKEN_ENV, "")
        if not expected:
            return JSONResponse(
                status_code=503,
                content={"detail": "PRICING_SERVICE_TOKEN is not configured"},
            )
        header = request.headers.get("authorization", "")
        prefix = "Bearer "
        presented = header[len(prefix):] if header.startswith(prefix) else ""
        if not presented or not hmac.compare_digest(presented, expected):
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
        return await call_next(request)

    @app.middleware("http")
    async def echo_idempotency(request: Request, call_next):
        response = await call_next(request)
        key = request.headers.get("idempotency-key")
        if key:
            response.headers["Idempotency-Key"] = key
        return response

    @app.get("/healthz", response_model=HealthOut)
    def healthz() -> HealthOut:
        return HealthOut(status="ok", engine_version=ENGINE_VERSION)

    @app.get("/v1/rulesets", response_model=RulesetListOut)
    def rulesets() -> Any:
        try:
            loaded = list_rulesets()
        except RulesetError as exc:
            return JSONResponse(status_code=500, content={"detail": str(exc)})
        try:
            default = active_version(utc_now())
        except RulesetError as exc:
            return JSONResponse(status_code=500, content={"detail": str(exc)})
        return RulesetListOut(
            engine_version=ENGINE_VERSION,
            default=default,
            rulesets=[
                {"version": item.version, "effective_from": item.raw["effective_from"]}
                for item in loaded
            ],
        )

    @app.get("/v1/rulesets/active", response_model=RulesetDetailOut)
    def ruleset_active() -> Any:
        try:
            loaded = load_ruleset(active_version(utc_now()))
        except RulesetError as exc:
            status = 404 if "no ruleset is effective" in str(exc) or "unknown ruleset" in str(exc) else 500
            return JSONResponse(status_code=status, content={"detail": str(exc)})
        return customer_ruleset_document(loaded, active=True)

    @app.get("/v1/rulesets/{version}", response_model=RulesetDetailOut)
    def ruleset_by_version(version: str) -> Any:
        try:
            loaded = load_ruleset(version)
        except RulesetError as exc:
            status = 404 if "not found" in str(exc) or "unknown ruleset" in str(exc) else 500
            return JSONResponse(status_code=status, content={"detail": str(exc)})
        try:
            current = active_version(utc_now())
        except RulesetError:
            current = None
        return customer_ruleset_document(loaded, active=loaded.version == current)

    @app.post("/v1/floors", response_model=FloorOut, dependencies=[Depends(_documented_bearer)])
    def floors(
        body: FloorIn,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Any:
        return _priced(price_floor, body.model_dump(exclude_none=True), idempotency_key, response)

    @app.post("/v1/rounds/realtime", response_model=RoundOut, dependencies=[Depends(_documented_bearer)])
    def realtime(
        body: RoundIn,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Any:
        return _priced(price_realtime, body.model_dump(exclude_none=True), idempotency_key, response)

    @app.post("/v1/rounds/day-ahead", response_model=RoundOut, dependencies=[Depends(_documented_bearer)])
    def day_ahead(
        body: RoundIn,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Any:
        return _priced(price_day_ahead, body.model_dump(exclude_none=True), idempotency_key, response)

    @app.post("/v1/token-prices", response_model=TokenPriceOut, dependencies=[Depends(_documented_bearer)])
    def token_prices(
        body: TokenPriceIn,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Any:
        return _priced(price_tokens, body.model_dump(exclude_none=True), idempotency_key, response)

    return app


def _priced(fn, body: dict[str, Any], idempotency_key: str | None, response: Response) -> Any:
    if idempotency_key is not None:
        if not idempotency_key or len(idempotency_key) > 200:
            return JSONResponse(
                status_code=422,
                content={"detail": "Idempotency-Key must be 1 to 200 characters"},
            )
        body = dict(body)
        body["idempotency_key"] = idempotency_key
        response.headers["Idempotency-Key"] = idempotency_key
    return _run(fn, body)


def _run(fn, body: dict[str, Any]) -> Any:
    if not isinstance(body, dict):
        return JSONResponse(status_code=422, content={"detail": "JSON object required"})
    try:
        return fn(body)
    except EngineError as exc:
        status = 404 if "not found" in str(exc) or "unknown ruleset" in str(exc) else 422
        return JSONResponse(status_code=status, content={"detail": str(exc)})


app = create_app()
