"""System diagnostics and telemetry API routes for TrackSeerr."""

from fastapi import APIRouter

from . import logs, status, tasks, updates

router = APIRouter()
for _module in (status, logs, tasks, updates):
    router.routes.extend(_module.router.routes)

__all__ = ["router"]
