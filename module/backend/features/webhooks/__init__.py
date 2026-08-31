"""GitHub Webhook integration package."""

from backend.features.webhooks.router import router as webhooks_router

__all__ = ["webhooks_router"]

