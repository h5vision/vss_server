"""Admin API aggregate router.

Combines domain-specific Admin sub-routers under the common /admin prefix.
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.features.admin.routers.audit import router as audit_router
from backend.features.admin.routers.bindings import router as bindings_router
from backend.features.admin.routers.commits import router as commits_router
from backend.features.admin.routers.repositories import router as repositories_router
from backend.features.admin.routers.snapshots import router as snapshots_router
from backend.features.admin.routers.tracked_branches import (
    router as tracked_branches_router,
)
from backend.features.admin.routers.vss import router as vss_router

router = APIRouter(prefix="/admin", tags=["admin"])

router.include_router(repositories_router)
router.include_router(tracked_branches_router)
router.include_router(bindings_router)
router.include_router(snapshots_router)
router.include_router(commits_router)
router.include_router(vss_router)
router.include_router(audit_router)
