"""System diagnostics and telemetry API routes for TrackSeerr."""

from fastapi import APIRouter

from . import health, logs, status, tasks, updates

router = APIRouter()
for _module in (status, logs, tasks, updates, health):
    router.routes.extend(_module.router.routes)

__all__ = ["router"]
