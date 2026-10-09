"""FastAPI router for native music library management.

Provides statistics and browsing CRUD for artists, albums, tracks, and files;
controls for filesystem scanning and Lidarr catalog migration;
interactive Manual Import scan and commit pipelines;
and Arr-grade token-template preview and batch-renaming engine.
"""

from fastapi import APIRouter
from . import albums, artists, browse, collections, manual_import, scan, tagging, tracks

router = APIRouter()
for _module in (browse, artists, albums, tracks, scan, manual_import, tagging, collections):
    router.include_router(_module.router)

__all__ = ["router"]
