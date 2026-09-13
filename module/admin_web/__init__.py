"""Independent Snapshot Admin Web and backend-for-frontend service."""

from admin_web.app import create_app
from admin_web.config import AdminWebSettings

__all__ = ["AdminWebSettings", "create_app"]

