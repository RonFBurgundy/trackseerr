"""System update checks and configuration API routes."""


from typing import (
    Any,
)
from fastapi import (
    APIRouter,
    Depends,
)
from trackseerr.api.schemas.system import (
    SystemUpdateResponse,
    UpdateSettingsPayload,
)
from trackseerr.update_check import (
    get_update_status,
)
from trackseerr.api.dependencies import (
    get_config,
    get_db,
    require_admin,
    require_core_tier,
)
from trackseerr.config import (
    Config,
)
from trackseerr.storage import Database



router = APIRouter()


@router.get(
    "/update",
    response_model=SystemUpdateResponse,
    response_model_exclude_unset=True,
    summary="TrackSeerr update status and check configuration",
    dependencies=[Depends(require_core_tier)],
)
def get_system_update(
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Returns current and latest version, release URL, timestamps, and update status (admin only)."""
    return get_update_status(db, config)


@router.put(
    "/update",
    response_model=SystemUpdateResponse,
    response_model_exclude_unset=True,
    summary="Enable or disable software update check",
    dependencies=[Depends(require_core_tier)],
)
def put_system_update(
    payload: UpdateSettingsPayload,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Updates the update_check_enabled setting (admin only)."""
    db.set_update_check_enabled(payload.enabled)
    return get_update_status(db, config)
