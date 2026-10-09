"""Admin-only deployment endpoints: gateway link status and the role-change banner.

Core and all-in-one only: both refuse with 404 on the gateway tier (and are never allow-listed there).
"""

from typing import Any

from fastapi import APIRouter, Depends

from trackseerr.api.dependencies import get_config, get_db, tier_of
from trackseerr.api.routes.admin_users import admin_core_user
from trackseerr.api.schemas.deployment import GatewayStatus, RoleChangeNotice
from trackseerr.config import Config
from trackseerr.gateway_link import compute_gateway_status
from trackseerr.role_change import current_notice, dismiss_notice
from trackseerr.storage import Database

router = APIRouter()


@router.get("/gateway-status", response_model=GatewayStatus, response_model_exclude_unset=True)
def gateway_status(
    _admin: dict[str, Any] = Depends(admin_core_user),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    public_url = (db.get_general_settings().get("application_url") or config.application_url or "").strip()
    return compute_gateway_status(db, role=tier_of(config), public_url=public_url)


@router.get("/role-change-notice", response_model=RoleChangeNotice, response_model_exclude_unset=True)
def get_role_change_notice(
    _admin: dict[str, Any] = Depends(admin_core_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    return current_notice(db)


@router.post("/role-change-notice/dismiss", response_model=RoleChangeNotice, response_model_exclude_unset=True)
def dismiss_role_change_notice(
    _admin: dict[str, Any] = Depends(admin_core_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    dismiss_notice(db)
    return current_notice(db)
