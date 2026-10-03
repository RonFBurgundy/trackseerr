"""FastAPI Application factory with security middleware and router registration."""

import logging
import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.staticfiles import StaticFiles

from plex_playlist_sync.api.routes import (
    account,
    acquisition,
    admin_users,
    auth,
    discovery,
    download_clients,
    indexers,
    internal,
    issues,
    library,
    missing,
    mixes,
    notifications,
    plex_playlists,
    playlists,
    quality_profiles,
    queue,
    requests,
    scrobbles,
    settings,
    sync,
    system,
    users,
)
from plex_playlist_sync.api.tier_middleware import INVITE_TOKEN_PATH_RE, GatewayGuardMiddleware, SignedBodyMiddleware
from plex_playlist_sync.config import Config
from plex_playlist_sync.internal_auth import MIN_SECRET_LENGTH, validate_secret_strength
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)


def _enforce_internal_secret(config: Optional[Config]) -> None:
    """Refuses to build a gateway/core app without a strong INTERNAL_CORE_SECRET."""
    if config is not None:
        role = (config.role or os.getenv("ROLE", "all-in-one")).lower().strip()
        secret = config.internal_core_secret
    else:
        role = os.getenv("ROLE", "all-in-one").lower().strip()
        secret = os.getenv("INTERNAL_CORE_SECRET", "").strip() or None
    if role in ("gateway", "core") and not validate_secret_strength(secret):
        raise RuntimeError(
            f"ROLE={role} requires INTERNAL_CORE_SECRET of at least {MIN_SECRET_LENGTH} characters"
        )


def _setup_logging_or_fail(config: Config) -> None:
    """Runs ``setup_logging``. A gateway/core must never run without log redaction, so it re-raises there."""
    try:
        from plex_playlist_sync.cli import setup_logging

        setup_logging(config.log_level, config=config)
    except (OSError, ImportError, ValueError, TypeError, AttributeError, RuntimeError) as exc:
        # Logging itself may be what failed, so report on stderr rather than through the logger.
        role = str(getattr(config, "role", "all-in-one") or "all-in-one")
        if role in ("gateway", "core"):
            print(f"FATAL: logging/redaction setup failed for ROLE={role}: {exc!r}", file=sys.stderr)
            raise
        print(f"WARNING: logging setup failed ({exc!r}); continuing without it (all-in-one)", file=sys.stderr)


def create_app(
    db: Optional[Database] = None,
    config: Optional[Config] = None,
) -> FastAPI:
    """Creates and configures a FastAPI application instance."""
    _enforce_internal_secret(config)
    docs_enabled = os.getenv("ENABLE_API_DOCS", "").strip() == "1"
    app = FastAPI(
        title="TrackSeerr API",
        version="1.0.0",
        docs_url="/api/docs" if docs_enabled else None,
        redoc_url="/api/redoc" if docs_enabled else None,
        openapi_url="/api/openapi.json" if docs_enabled else None,
    )

    # Attach instances to app state if provided
    if db is not None:
        app.state.db = db
    if config is not None:
        app.state.config = config
        _setup_logging_or_fail(config)

    # 0. Two-tier security: hash signed bodies (all roles); deny-by-default guard (gateway role only, checked per request)
    # Added last = outermost: forged X-TS-* headers are refused on a gateway before the guard forwards anything.
    app.add_middleware(GatewayGuardMiddleware)
    app.add_middleware(SignedBodyMiddleware)

    # 1. Security Headers Middleware
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next) -> Response:
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-eval' 'unsafe-inline' "
            "https://cdn.tailwindcss.com https://unpkg.com https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https:; "
            "media-src 'self' https: data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        return response

    cors_origins_env = os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5250,http://127.0.0.1:5250").strip()
    if cors_origins_env:
        if cors_origins_env == "*":
            # Wildcard origin cannot be used with credentials
            app.add_middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_credentials=False,
                allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                allow_headers=["*"],
            )
        else:
            origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
            app.add_middleware(
                CORSMiddleware,
                allow_origins=origins,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )

    # 3. Mount Routers under /api
    api_router = APIRouter(prefix="/api")
    api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
    api_router.include_router(account.router, prefix="/account", tags=["account"])
    api_router.include_router(internal.router, prefix="/internal", tags=["internal"])
    api_router.include_router(users.router, prefix="/users", tags=["users"])
    api_router.include_router(admin_users.router, prefix="/admin", tags=["admin-users"])
    api_router.include_router(playlists.router, prefix="/playlists", tags=["playlists"])
    api_router.include_router(plex_playlists.router, prefix="/plex-playlists", tags=["plex_playlists"])
    api_router.include_router(sync.router, prefix="/sync", tags=["sync"])
    api_router.include_router(missing.router, prefix="/missing", tags=["missing"])
    api_router.include_router(discovery.router, prefix="/discovery", tags=["discovery"])
    api_router.include_router(requests.router, prefix="/requests", tags=["requests"])
    api_router.include_router(issues.router, prefix="/issues", tags=["issues"])
    api_router.include_router(library.router, prefix="/library", tags=["library"])
    api_router.include_router(settings.router, prefix="/settings", tags=["settings"])
    api_router.include_router(
        download_clients.router, prefix="/settings/download-clients", tags=["download_clients"]
    )
    api_router.include_router(indexers.router, prefix="/settings/indexers", tags=["indexers"])
    api_router.include_router(
        quality_profiles.router, prefix="/settings/quality-profiles", tags=["quality_profiles"]
    )
    api_router.include_router(
        notifications.router, prefix="/settings/notifications", tags=["notifications"]
    )
    api_router.include_router(queue.router, prefix="/queue", tags=["queue"])
    api_router.include_router(acquisition.router, prefix="/acquisition", tags=["acquisition"])
    api_router.include_router(scrobbles.router, prefix="/scrobbles", tags=["scrobbles"])
    api_router.include_router(mixes.router, prefix="/mixes", tags=["mixes"])
    api_router.include_router(system.router, prefix="/system", tags=["system"])

    @api_router.api_route("/health", methods=["GET", "HEAD"], tags=["health"])
    def health_check() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(api_router)

    # 4. Mount Static Directory & SPA Assets & Serve Root
    static_dir = Path(__file__).resolve().parent.parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Locate SPA dist
    project_root = Path(__file__).resolve().parent.parent.parent
    dist_dir = project_root / "frontend" / "dist"
    if not dist_dir.is_dir():
        dist_dir = static_dir / "dist"

    assets_dir = dist_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    # Serve root public PWA assets
    for public_name in [
        "manifest.json",
        "favicon.svg",
        "favicon.png",
        "apple-touch-icon.png",
        "icon-192.png",
        "icon-512.png",
        "trackseerr-logo.svg",
        "placeholder.svg",
    ]:
        def _make_public_handler(name: str):
            def handler() -> FileResponse:
                dist_target = dist_dir / name
                if dist_target.is_file():
                    return FileResponse(str(dist_target))
                static_target = static_dir / name
                if static_target.is_file():
                    return FileResponse(str(static_target))
                raise HTTPException(status_code=404, detail="Not Found")
            return handler

        app.add_api_route(
            f"/{public_name}",
            _make_public_handler(public_name),
            methods=["GET", "HEAD"],
            include_in_schema=False,
        )

    @app.api_route("/", methods=["GET", "HEAD"], response_class=FileResponse, include_in_schema=False)
    def serve_index() -> FileResponse:
        dist_index = dist_dir / "index.html"
        use_legacy = os.environ.get("TRACKSEERR_LEGACY_UI") == "1"
        if not use_legacy and dist_index.is_file():
            return FileResponse(str(dist_index), media_type="text/html")
        return FileResponse(str(static_dir / "index.html"), media_type="text/html")

    @app.api_route(
        "/invite/{token}",
        methods=["GET", "HEAD"],
        response_class=FileResponse,
        include_in_schema=False,
    )
    def serve_invite_page(token: str) -> FileResponse:
        """SPA entry for the invite / password-reset link. Only the exact ``/invite/<urlsafe token>`` shape."""
        if not INVITE_TOKEN_PATH_RE.fullmatch(token):
            raise HTTPException(status_code=404, detail="Not Found")
        dist_index = dist_dir / "index.html"
        use_legacy = os.environ.get("TRACKSEERR_LEGACY_UI") == "1"
        target = dist_index if (not use_legacy and dist_index.is_file()) else static_dir / "index.html"
        # The URL carries a secret: keep the page out of caches.
        return FileResponse(str(target), media_type="text/html", headers={"Cache-Control": "no-store"})

    return app


app = create_app()
