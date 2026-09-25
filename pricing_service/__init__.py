"""Optional HTTP service in front of pricing_core. Depends on FastAPI."""

from pricing_service.app import create_app

__all__ = ["create_app"]
