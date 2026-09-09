"""Compatibility entrypoint for `uvicorn main:app`."""

from backend.app import app

__all__ = ["app"]
