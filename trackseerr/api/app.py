"""FastAPI Application factory with security middleware and router registration."""

import json
import logging
import sqlite3
import os
import sys
from pathlib import Path
from typing import Optional

from fastapi.exceptions import ResponseValidationError
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from trackseerr import __version__
from trackseerr.boot import boot_state
from trackseerr.media_server import MediaServerUnavailable, media_server_unavailable_response
from trackseerr.api.routes import (
    account,
    acquisition,
    activity,
    admin_users,
    auth,
    changelog,
    backups,
    deployment,
    discovery,
    download_clients,
    import_lists,
    tags,
    indexers,
    internal,
    calendar,
    issues,
    itunes_import,
    lidarr_compat,
    library,
    library_health,
    seed_cleanup,
    recycle_bin,
    missing,
    mixes,
    notifications,
    plex_playlists,
    playlists,
    quality_catalog,
    delay_profiles,
    quality_profiles,
    queue,
    requests,
    scrobbles,
    settings,
    sync,
    system,
    user_notifications,
    users,
    wanted,
)
from trackseerr.api.tier_middleware import (
    INVITE_TOKEN_PATH_RE,
    GatewayGuardMiddleware,
    SignedBodyMiddleware,
    SECURITY_HEADERS,
    StartupGateMiddleware,
)
from trackseerr.api.dependencies import register_db
from trackseerr.config import Config
from trackseerr.internal_auth import MIN_SECRET_LENGTH, validate_secret_strength
from trackseerr.role_guard import (
    CODE_APP_URL_MISSING,
    CODE_CORE_URL,
    CODE_DB_PRESENT,
    CODE_SECRET_WEAK,
    README_HINT,
    check_role_environment,
)
from trackseerr.storage import Database

logger = logging.getLogger(__name__)


class StartupRefusal(RuntimeError):
    """A startup guardrail refusal; ``lines`` holds one message per problem for clean stderr output."""

    def __init__(self, lines: list[str]) -> None:
        super().__init__(" ".join(lines))
        self.lines = lines


def _enforce_internal_secret(config: Optional[Config]) -> None:
    """Refuses to build a gateway/core app without a strong INTERNAL_CORE_SECRET."""
    if config is not None:
        role = (config.role or os.getenv("ROLE", "all-in-one")).lower().strip()
        secret = config.internal_core_secret
    else:
        role = os.getenv("ROLE", "all-in-one").lower().strip()
        secret = os.getenv("INTERNAL_CORE_SECRET", "").strip() or None
    if role in ("gateway", "core") and not validate_secret_strength(secret):
        raise StartupRefusal(
            [f"ROLE={role} requires INTERNAL_CORE_SECRET of at least {MIN_SECRET_LENGTH} characters", README_HINT]
        )


# With an explicit Config the caller validated the process environment (URLs, on-disk
# databases) and ``_enforce_internal_secret`` checks the Config's secret; only the forbidden-env rule is
# re-checked here. ``create_app()`` with no Config (the ``uvicorn trackseerr.api.app:app`` path) runs every rule.
_CLI_OWNED_CODES = frozenset({CODE_APP_URL_MISSING, CODE_CORE_URL, CODE_DB_PRESENT, CODE_SECRET_WEAK})


def _enforce_role_environment(config: Optional[Config]) -> None:
    """Refuses to build a gateway/core app whose environment breaks the DMZ guardrails."""
    role = ((config.role if config is not None else None) or os.getenv("ROLE", "all-in-one")).lower().strip()
    if role not in ("gateway", "core"):
        return
    problems = [p for p in check_role_environment(role, os.environ) if p.fatal]
    if config is not None:
        problems = [p for p in problems if p.code not in _CLI_OWNED_CODES]
    if problems:
        raise StartupRefusal([f"ROLE={role} refused to start: {p.message}" for p in problems] + [README_HINT])


def _load_manifest(dist_dir: Path, static_dir: Path) -> Optional[dict]:
    for candidate in (dist_dir / "manifest.json", static_dir / "manifest.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _setup_logging_or_fail(config: Config) -> None:
    """Runs ``setup_logging``. A gateway/core must never run without log redaction, so it re-raises there."""
    try:
        from trackseerr.cli import setup_logging

        setup_logging(config.log_level, config=config)
    except (OSError, ImportError, ValueError, TypeError, AttributeError, RuntimeError) as exc:
        # Logging itself may be what failed, so report on stderr rather than through the logger.
        role = str(getattr(config, "role", "all-in-one") or "all-in-one")
        if role in ("gateway", "core"):
            print(f"FATAL: logging/redaction setup failed for ROLE={role}: {exc!r}", file=sys.stderr)
            raise
        print(f"WARNING: logging setup failed ({exc!r}); continuing without it (all-in-one)", file=sys.stderr)


def _role_of(app: FastAPI) -> str:
    cfg = getattr(app.state, "config", None)
    role = ((cfg.role if cfg is not None else None) or os.getenv("ROLE", "all-in-one")).lower().strip()
    return role if role in ("gateway", "core") else "all-in-one"


def response_validation_error_response(request: Request, exc: Exception) -> JSONResponse:
    """A handler produced data its response model rejects: log shape (never values), return a generic 500."""
    errors = exc.errors() if isinstance(exc, ResponseValidationError) else []
    summary = "; ".join(
        f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('type')}: {e.get('msg')}" for e in errors[:20]
    )
    logger.error("Response validation failed for %s %s: %s", request.method, request.url.path, summary)
    return JSONResponse(status_code=500, content={"detail": "Internal response error"})


def create_app(
    db: Optional[Database] = None,
    config: Optional[Config] = None,
) -> FastAPI:
    """Creates and configures a FastAPI application instance."""
    _enforce_internal_secret(config)
    _enforce_role_environment(config)
    docs_enabled = os.getenv("ENABLE_API_DOCS", "").strip() == "1"
    app = FastAPI(
        title="TrackSeerr API",
        version=__version__,
        docs_url="/api/docs" if docs_enabled else None,
        redoc_url="/api/redoc" if docs_enabled else None,
        openapi_url="/api/openapi.json" if docs_enabled else None,
    )

    # Attach instances to app state if provided
    if db is not None:
        app.state.db = db
        register_db(db)
        role = ((config.role if config is not None else None) or os.getenv("ROLE", "all-in-one")).lower().strip()
        if role == "gateway":
            try:
                db.set_last_role("gateway")
            except sqlite3.Error as exc:
                logger.warning("Could not record the gateway role: %s", type(exc).__name__)
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
        for header, value in SECURITY_HEADERS.items():
            # A route that set its own header (e.g. the artwork proxy's locked-down CSP) keeps it.
            if header not in response.headers:
                response.headers[header] = value
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

    app.add_exception_handler(MediaServerUnavailable, media_server_unavailable_response)
    app.add_exception_handler(ResponseValidationError, response_validation_error_response)

    # 3. Mount Routers under /api
    api_router = APIRouter(prefix="/api")
    api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
    api_router.include_router(account.router, prefix="/account", tags=["account"])
    api_router.include_router(
        user_notifications.router, prefix="/account/notifications", tags=["account-notifications"]
    )
    api_router.include_router(internal.router, prefix="/internal", tags=["internal"])
    api_router.include_router(users.router, prefix="/users", tags=["users"])
    api_router.include_router(admin_users.router, prefix="/admin", tags=["admin-users"])
    api_router.include_router(deployment.router, prefix="/admin", tags=["deployment"])
    api_router.include_router(playlists.router, prefix="/playlists", tags=["playlists"])
    api_router.include_router(import_lists.router, prefix="/import-lists", tags=["import-lists"])
    api_router.include_router(plex_playlists.router, prefix="/plex-playlists", tags=["plex_playlists"])
    api_router.include_router(sync.router, prefix="/sync", tags=["sync"])
    api_router.include_router(missing.router, prefix="/missing", tags=["missing"])
    api_router.include_router(discovery.router, prefix="/discovery", tags=["discovery"])
    api_router.include_router(requests.router, prefix="/requests", tags=["requests"])
    api_router.include_router(issues.router, prefix="/issues", tags=["issues"])
    api_router.include_router(library.router, prefix="/library", tags=["library"])
    api_router.include_router(library_health.router, prefix="/library-health", tags=["library-health"])
    api_router.include_router(seed_cleanup.router, prefix="/seed-cleanup", tags=["seed-cleanup"])
    api_router.include_router(recycle_bin.router, prefix="/recycle-bin", tags=["recycle-bin"])
    api_router.include_router(itunes_import.router, prefix="/import/itunes", tags=["itunes-import"])
    api_router.include_router(tags.router, prefix="/tags", tags=["tags"])
    api_router.include_router(settings.router, prefix="/settings", tags=["settings"])
    api_router.include_router(
        download_clients.router, prefix="/settings/download-clients", tags=["download_clients"]
    )
    api_router.include_router(indexers.router, prefix="/settings/indexers", tags=["indexers"])
    api_router.include_router(
        quality_profiles.router, prefix="/settings/quality-profiles", tags=["quality_profiles"]
    )
    api_router.include_router(
        quality_catalog.definitions_router, prefix="/settings/quality-definitions", tags=["quality_definitions"]
    )
    api_router.include_router(
        quality_catalog.formats_router, prefix="/settings/custom-formats", tags=["custom_formats"]
    )
    api_router.include_router(
        quality_catalog.release_profiles_router, prefix="/settings/release-profiles", tags=["release_profiles"]
    )
    api_router.include_router(delay_profiles.router, prefix="/settings/delay-profiles", tags=["delay_profiles"])
    api_router.include_router(
        notifications.router, prefix="/settings/notifications", tags=["notifications"]
    )
    api_router.include_router(queue.router, prefix="/queue", tags=["queue"])
    api_router.include_router(acquisition.router, prefix="/acquisition", tags=["acquisition"])
    api_router.include_router(scrobbles.router, prefix="/scrobbles", tags=["scrobbles"])
    api_router.include_router(mixes.router, prefix="/mixes", tags=["mixes"])
    api_router.include_router(system.router, prefix="/system", tags=["system"])
    api_router.include_router(changelog.router, prefix="/system/changelog", tags=["changelog"])
    api_router.include_router(backups.router, prefix="/system/backups", tags=["backups"])
    api_router.include_router(activity.router, prefix="/activity", tags=["activity"])
    api_router.include_router(wanted.router, prefix="/wanted", tags=["wanted"])
    api_router.include_router(calendar.router, prefix="/calendar", tags=["calendar"])
    api_router.include_router(
        lidarr_compat.router, prefix="/v1", tags=["lidarr-compat"], include_in_schema=False
    )

    @api_router.api_route("/health", methods=["HEAD"], include_in_schema=False)
    @api_router.get("/health", tags=["health"])
    def health_check() -> dict[str, object]:
        # Deliberately minimal and unauthenticated: tier and boot state only, never a version.
        # Always 200 so a container HEALTHCHECK does not flap while booting; the SPA reads ``status``.
        return {**boot_state.snapshot(detailed=_role_of(app) != "gateway"), "tier": _role_of(app)}

    @api_router.api_route("/health/ready", methods=["HEAD"], include_in_schema=False)
    @api_router.get("/health/ready", tags=["health"])
    def health_ready() -> JSONResponse:
        # Readiness probe: 503 + Retry-After until startup has finished.
        body = {**boot_state.snapshot(detailed=_role_of(app) != "gateway"), "tier": _role_of(app)}
        if boot_state.ready:
            return JSONResponse(body)
        return JSONResponse(body, status_code=503, headers={"Retry-After": "5"})

    app.include_router(api_router)

    # Outermost: while the process is still booting, every /api route except health answers 503.
    app.add_middleware(StartupGateMiddleware)

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
    base_manifest = _load_manifest(dist_dir, static_dir)

    @app.api_route("/manifest.json", methods=["GET", "HEAD"], include_in_schema=False)
    def serve_manifest() -> Response:
        """Role-aware PWA manifest: the gateway installs as "TrackSeerr Requests"."""
        if base_manifest is None:
            raise HTTPException(status_code=404, detail="Not Found")
        manifest = dict(base_manifest)
        if _role_of(app) == "gateway":
            manifest["name"] = "TrackSeerr Requests"
            manifest["short_name"] = "Requests"
        else:
            manifest["name"] = "TrackSeerr"
            manifest["short_name"] = "TrackSeerr"
        return JSONResponse(manifest, media_type="application/manifest+json")

    for public_name in [
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


def __getattr__(name: str) -> FastAPI:
    """Builds the module-level ``app`` lazily (PEP 562) so importing this module never runs the guardrails.

    ``uvicorn trackseerr.api.app:app`` triggers this; a refusal prints clean lines and exits 1.
    """
    if name != "app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        built = create_app()
    except StartupRefusal as exc:
        for line in exc.lines:
            print(f"ERROR: {line}", file=sys.stderr)
        raise SystemExit(1) from None
    globals()["app"] = built
    return built
