"""Stateless pricing HTTP API.

Auth is a bearer token from ``PRICING_SERVICE_TOKEN``. The process refuses
``/v1`` calls when that variable is unset. ``/healthz`` stays open so a
probe can see a process that has not been given its token yet. The token
is never logged and is not a configuration file.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pricing_core import ENGINE_VERSION
from pricing_core.engine import EngineError, price_day_ahead, price_floor, price_realtime
from pricing_core.ruleset import RulesetError, active_version, list_rulesets, load_ruleset
from pricing_core.transparency import ruleset_document, utc_now
from pricing_service.schemas import (
    FloorIn,
    FloorOut,
    HealthOut,
    RoundIn,
    RoundOut,
    RulesetDetailOut,
    RulesetListOut,
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


def create_app() -> FastAPI:
    app = FastAPI(
        title="Hypermesh pricing",
        version=ENGINE_VERSION,
        description=(
            "Stateless internal pricing engine. Tuning values in the published "
            "ruleset are Simulated (market-sim seed 5547), not measured Fyber prices. "
            "The platform fee is a per-request take; the ruleset does not set one. "
            "GET /v1/rulesets/active is the customer-facing rules card: show "
            "public_summary and parameters marked customer_visible."
        ),
        openapi_url="/openapi.json",
    )

    @app.middleware("http")
    async def service_token(request: Request, call_next):
        if request.url.path == "/healthz" or request.url.path in {"/openapi.json", "/docs", "/redoc"}:
            return await call_next(request)
        if not request.url.path.startswith("/v1/"):
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

    @app.get("/healthz", response_model=HealthOut)
    def healthz() -> HealthOut:
        return HealthOut(status="ok", engine_version=ENGINE_VERSION)

    @app.get("/v1/rulesets", response_model=RulesetListOut, dependencies=[Depends(_documented_bearer)])
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
                {"version": item.version, "sha256": item.sha256, "source": item.source}
                for item in loaded
            ],
        )

    @app.get(
        "/v1/rulesets/active",
        response_model=RulesetDetailOut,
        dependencies=[Depends(_documented_bearer)],
    )
    def ruleset_active() -> Any:
        try:
            loaded = load_ruleset(active_version(utc_now()))
        except RulesetError as exc:
            status = 404 if "no ruleset is effective" in str(exc) or "unknown ruleset" in str(exc) else 500
            return JSONResponse(status_code=status, content={"detail": str(exc)})
        return ruleset_document(loaded, active=True)

    @app.get(
        "/v1/rulesets/{version}",
        response_model=RulesetDetailOut,
        dependencies=[Depends(_documented_bearer)],
    )
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
        return ruleset_document(loaded, active=loaded.version == current)

    @app.post("/v1/floors", response_model=FloorOut, dependencies=[Depends(_documented_bearer)])
    def floors(body: FloorIn) -> Any:
        return _run(price_floor, body.model_dump(exclude_none=True))

    @app.post("/v1/rounds/realtime", response_model=RoundOut, dependencies=[Depends(_documented_bearer)])
    def realtime(body: RoundIn) -> Any:
        return _run(price_realtime, body.model_dump(exclude_none=True))

    @app.post("/v1/rounds/day-ahead", response_model=RoundOut, dependencies=[Depends(_documented_bearer)])
    def day_ahead(body: RoundIn) -> Any:
        return _run(price_day_ahead, body.model_dump(exclude_none=True))

    return app


def _run(fn, body: dict[str, Any]) -> Any:
    if not isinstance(body, dict):
        return JSONResponse(status_code=422, content={"detail": "JSON object required"})
    try:
        return fn(body)
    except EngineError as exc:
        status = 404 if "not found" in str(exc) or "unknown ruleset" in str(exc) else 422
        return JSONResponse(status_code=status, content={"detail": str(exc)})


app = create_app()
