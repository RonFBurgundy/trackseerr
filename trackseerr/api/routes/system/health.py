"""System health checks and configuration API routes."""

from typing import Any
from fastapi import APIRouter, Depends

from trackseerr.api.dependencies import (
    get_db,
    require_admin,
    require_core_tier,
)
from trackseerr.api.schemas.system import (
    HealthSettings,
    SystemHealthResponse,
)
from trackseerr.storage import Database

router = APIRouter()


@router.get(
    "/health",
    response_model=SystemHealthResponse,
    response_model_exclude_unset=True,
    summary="List all system health check states",
    dependencies=[Depends(require_core_tier)],
)
def get_system_health(
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Returns all system health check probe states and latest run timestamp (admin only)."""
    raw_checks = db.list_health_checks()
    checks: list[dict[str, Any]] = []
    checked_times: list[str] = []
    for c in raw_checks:
        last_checked = c.get("last_checked_at")
        if last_checked:
            checked_times.append(last_checked)
        checks.append(
            {
                "key": c.get("check_key") or c.get("key"),
                "kind": c.get("kind"),
                "label": c.get("label"),
                "state": c.get("state"),
                "message": c.get("message"),
                "failing_since": c.get("failing_since"),
                "last_checked_at": last_checked,
            }
        )
    checked_at = max(checked_times) if checked_times else None
    return {
        "checks": checks,
        "checked_at": checked_at,
    }


@router.get(
    "/health/settings",
    response_model=HealthSettings,
    response_model_exclude_unset=True,
    summary="Get system health check settings",
    dependencies=[Depends(require_core_tier)],
)
def get_health_settings(
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Returns current health check thresholds and settings (admin only)."""
    return db.get_health_settings()


@router.put(
    "/health/settings",
    response_model=HealthSettings,
    response_model_exclude_unset=True,
    summary="Update system health check settings",
    dependencies=[Depends(require_core_tier)],
)
def put_health_settings(
    payload: HealthSettings,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Updates health check thresholds and settings (admin only)."""
    return db.set_health_settings(payload.model_dump())
