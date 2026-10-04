"""Media Management Settings and Token Preview API endpoints."""

import logging
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from plex_playlist_sync import library_manager, lidarr_library
from plex_playlist_sync.api.dependencies import get_config, get_db, require_admin, require_core_tier
from plex_playlist_sync.config import Config
from plex_playlist_sync.library_monitoring import validate_monitor_option
from plex_playlist_sync.redaction import redact_text
from plex_playlist_sync.clients.lidarr import LidarrApiError, LidarrClient, invalidate_add_defaults
from plex_playlist_sync.naming import (
    PRESET_DESCRIPTIONS,
    PRESETS,
    SYNTAX_HELP,
    TOKEN_HELP,
    build_path_parts,
    build_track_path,
    resolve_track_formats,
    validate_format,
)
from plex_playlist_sync.security import is_safe_service_url, mask_secret
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter()

# Default preview sample items representing real-world library scenarios
SAMPLE_PREVIEW_ITEMS: list[dict[str, Any]] = [
    {
        "id": "standard",
        "name": "Standard Single-Disc Track",
        "description": "Standard studio album track",
        "metadata": {
            "artist": "Pink Floyd",
            "album": "The Dark Side of the Moon",
            "title": "Speak to Me",
            "release_year": 1973,
            "year": 1973,
            "track_number": 1,
            "disc_number": 1,
            "total_discs": 1,
            "codec": "FLAC",
            "bit_depth": 24,
            "bits_per_sample": 24,
            "sample_rate": 96000,
            "extension": ".flac",
            "quality_full": "FLAC 24bit 96kHz",
        },
    },
    {
        "id": "multi_disc",
        "name": "Multi-Disc Track (Disc 2)",
        "description": "Track on second disc of a multi-disc set",
        "metadata": {
            "artist": "The Beatles",
            "album": "The Beatles (White Album)",
            "title": "Revolution 1",
            "release_year": 1968,
            "year": 1968,
            "track_number": 1,
            "disc_number": 2,
            "total_discs": 2,
            "medium_format": "CD",
            "codec": "FLAC",
            "bit_depth": 16,
            "bits_per_sample": 16,
            "sample_rate": 44100,
            "extension": ".flac",
            "quality_full": "FLAC 16bit 44.1kHz",
        },
    },
    {
        "id": "compilation",
        "name": "Compilation / Various Artists Track",
        "description": "Track with distinct artist on a Various Artists compilation",
        "metadata": {
            "artist": "Queen",
            "album_artist": "Various Artists",
            "album": "Wayne's World: Music from the Motion Picture",
            "title": "Bohemian Rhapsody",
            "release_year": 1992,
            "year": 1992,
            "album_type": "Soundtrack",
            "track_number": 1,
            "disc_number": 1,
            "total_discs": 1,
            "is_compilation": True,
            "codec": "MP3",
            "bitrate": "320kbps",
            "extension": ".mp3",
            "quality_full": "MP3 320kbps",
        },
    },
    {
        "id": "typed_release",
        "name": "Typed Release (EP)",
        "description": "Release with an album type and disambiguation, to exercise optional {Album Type} blocks",
        "metadata": {
            "artist": "Boards of Canada",
            "album": "Twoism",
            "title": "Basefree",
            "album_type": "EP",
            "album_disambiguation": "Remastered",
            "release_year": 1995,
            "year": 1995,
            "track_number": 3,
            "disc_number": 1,
            "total_discs": 1,
            "codec": "FLAC",
            "bit_depth": 16,
            "bits_per_sample": 16,
            "sample_rate": 44100,
            "extension": ".flac",
            "quality_full": "FLAC 16bit 44.1kHz",
        },
    },
]


class GeneralSettingsModel(BaseModel):
    application_url: str = Field("", description="External application URL for redirects and notifications")
    updated_at: str | None = None


class GeneralSettingsUpdateModel(BaseModel):
    application_url: str | None = Field(None, description="External application URL (e.g. https://trackseerr.mydomain.com)")


class MediaManagementSettingsModel(BaseModel):
    artist_folder_format: str = Field(..., description="Format for artist directory")
    album_folder_format: str = Field(..., description="Legacy album directory format (superseded by the track formats)")
    standard_track_format: str = Field(
        ..., description="Standard track format: '/'-separated path below the artist folder (album folder(s)/file name)"
    )
    multi_disc_track_format: str = Field(
        "", description="Multi-disc track format: same as the standard format but for releases with more than one disc"
    )
    compilation_track_format: str = Field(..., description="Format for compilation track filenames")
    multi_disc_folder_format: str = Field(..., description="Legacy multi-disc subdirectory format (superseded)")
    root_folder_path: str = Field("/data/media/music", description="Base music library folder")
    colon_replacement_format: str = Field(" - ", description="String to replace colons with")
    clean_artist_names: bool = Field(True, description="Whether to strip leading articles from artist names")
    staging_folder_path: str = Field("/data/downloads", description="Path for staging/downloads folder")
    import_mode: str = Field("move", description="Import mode: move or hardlink")
    write_audio_tags: bool = Field(True, description="Whether to normalize audio tags on import")
    embed_artwork: bool = Field(True, description="Whether to embed cover artwork in audio files")
    save_cover_art_file: bool = Field(True, description="Whether to save cover.jpg in album directory")
    delete_completed_transfers: bool = Field(False, description="Whether to delete completed transfers from client")
    enable_quality_upgrades: bool = Field(True, description="Whether to monitor for quality cutoff upgrades")
    library_mode: str = Field("native", description="Library management mode: native or lidarr")
    seed_ratio_limit: float | None = Field(None, description="Target seed ratio before transfer cleanup")
    seed_time_limit_minutes: int | None = Field(None, description="Target seeding duration in minutes before transfer cleanup")
    enrich_mbids: bool = Field(True, description="Whether to enrich tracks and albums with MusicBrainz IDs")
    acoustid_api_key: str | None = Field(None, description="AcoustID API key for Chromaprint fingerprinting")
    mb_mirror_url: str = Field("https://api.brainzmash.cc", description="MusicBrainz / BrainzMash API mirror base URL")
    prefer_local_artwork: bool = Field(True, description="Whether to prefer local filesystem artwork over remote metadata art")
    scan_monitor_option: str = Field("existing", description="Monitor option given to artists created by a library scan")
    add_monitor_option: str = Field("all", description="Default monitor option for artists added manually")
    updated_at: str | None = None


class MediaManagementUpdateModel(BaseModel):
    artist_folder_format: str | None = None
    album_folder_format: str | None = None
    standard_track_format: str | None = None
    compilation_track_format: str | None = None
    multi_disc_folder_format: str | None = None
    multi_disc_track_format: str | None = None
    root_folder_path: str | None = None
    colon_replacement_format: str | None = None
    clean_artist_names: bool | None = None
    staging_folder_path: str | None = None
    import_mode: str | None = None
    write_audio_tags: bool | None = None
    embed_artwork: bool | None = None
    save_cover_art_file: bool | None = None
    delete_completed_transfers: bool | None = None
    enable_quality_upgrades: bool | None = None
    library_mode: str | None = None
    seed_ratio_limit: float | None = None
    seed_time_limit_minutes: int | None = None
    enrich_mbids: bool | None = None
    acoustid_api_key: str | None = None
    mb_mirror_url: str | None = None
    prefer_local_artwork: bool | None = None
    scan_monitor_option: str | None = None
    add_monitor_option: str | None = None

    @field_validator("scan_monitor_option", "add_monitor_option")
    @classmethod
    def _check_monitor_option(cls, value: str | None) -> str | None:
        return validate_monitor_option(value) if value is not None else None


class PreviewRequestModel(BaseModel):
    artist_folder_format: str | None = None
    album_folder_format: str | None = None
    standard_track_format: str | None = None
    compilation_track_format: str | None = None
    multi_disc_folder_format: str | None = None
    multi_disc_track_format: str | None = None
    root_folder_path: str | None = None
    colon_replacement_format: str | None = None
    clean_artist_names: bool | None = None
    staging_folder_path: str | None = None
    import_mode: str | None = None
    delete_completed_transfers: bool | None = None
    enable_quality_upgrades: bool | None = None
    library_mode: str | None = None


class PreviewItemModel(BaseModel):
    id: str
    name: str
    description: str
    metadata: dict[str, Any]
    output_path: str


class FormatSamplePreviewModel(BaseModel):
    sample_id: str
    sample_name: str
    output: str


class FormatPreviewModel(BaseModel):
    """How a single format string renders for every sample input, plus lint warnings."""

    format: str
    warnings: list[str] = Field(default_factory=list)
    samples: list[FormatSamplePreviewModel] = Field(default_factory=list)


class PreviewResponseModel(BaseModel):
    previews: list[PreviewItemModel]
    format_previews: dict[str, FormatPreviewModel] = Field(default_factory=dict)




class LidarrSettingsModel(BaseModel):
    url: str | None = None
    api_key: str | None = None
    auto_search: bool = True
    root_folder: str | None = None
    trickle_rate_seconds: float = 3.0
    trickle_batch_size: int = 25
    auto_trickle: bool = False
    auto_trickle_interval_minutes: int = 30
    search_on_add: bool = True
    prefer_singles: bool = True
    updated_at: str | None = None


class LidarrSettingsUpdateModel(BaseModel):
    """Unknown fields are ignored on purpose: older clients may still send the removed monitor / profile / tag
    overrides (Lidarr's root-folder defaults decide those now)."""

    url: str | None = None
    api_key: str | None = None
    auto_search: bool | None = None
    root_folder: str | None = None
    trickle_rate_seconds: float | None = None
    trickle_batch_size: int | None = None
    auto_trickle: bool | None = None
    auto_trickle_interval_minutes: int | None = None
    search_on_add: bool | None = None
    prefer_singles: bool | None = None


class LidarrNamedProfile(BaseModel):
    id: int
    name: str


class LidarrTagModel(BaseModel):
    id: int
    label: str


class LidarrDefaultsResponse(BaseModel):
    root_folder: str
    quality_profile: LidarrNamedProfile
    metadata_profile: LidarrNamedProfile
    monitor: str
    new_item_monitor: str
    tags: list[LidarrTagModel]
    source: Literal["rootfolder", "fallback"]
    root_folders: list[str]
    singles_enabled: bool = True


class LidarrTestConnectionPayload(BaseModel):
    url: str
    api_key: str


class LidarrTestConnectionResponse(BaseModel):
    online: bool
    version: str | None = None
    error: str | None = None


class MediaManagementGetResponse(BaseModel):
    settings: MediaManagementSettingsModel
    presets: dict[str, dict[str, Any]]
    preset_descriptions: dict[str, str] = Field(default_factory=dict)
    token_help: list[dict[str, Any]] = Field(default_factory=list)
    syntax_help: list[dict[str, str]] = Field(default_factory=list)


def _render_previews_for_settings(settings: dict[str, Any]) -> list[PreviewItemModel]:
    """Generates preview items in-memory without any filesystem operations."""
    items: list[PreviewItemModel] = []
    for sample in SAMPLE_PREVIEW_ITEMS:
        out_path = build_track_path(sample["metadata"], settings)
        items.append(
            PreviewItemModel(
                id=sample["id"],
                name=sample["name"],
                description=sample["description"],
                metadata=sample["metadata"],
                output_path=out_path,
            )
        )
    return items


def _render_format_previews(settings: dict[str, Any]) -> dict[str, FormatPreviewModel]:
    """Renders each of the three formats against every sample input (in-memory, no filesystem access)."""
    standard_fmt, multi_fmt = resolve_track_formats(settings)
    artist_fmt = str(settings.get("artist_folder_format") or "")
    specs: list[tuple[str, str, str]] = [
        ("artist_folder_format", artist_fmt, "artist"),
        ("standard_track_format", standard_fmt, "track"),
        ("multi_disc_track_format", multi_fmt, "track"),
    ]
    result: dict[str, FormatPreviewModel] = {}
    for key, fmt, kind in specs:
        samples: list[FormatSamplePreviewModel] = []
        for sample in SAMPLE_PREVIEW_ITEMS:
            if key == "artist_folder_format":
                parts = build_path_parts(sample["metadata"], settings)
                output = parts["artist"]
            else:
                parts = build_path_parts(sample["metadata"], settings, multi_disc=(key == "multi_disc_track_format"))
                output = "/".join([*parts["folders"], parts["file"]])
            samples.append(FormatSamplePreviewModel(sample_id=sample["id"], sample_name=sample["name"], output=output))
        result[key] = FormatPreviewModel(format=fmt, warnings=validate_format(fmt, kind), samples=samples)
    return result


@router.get(
    "/media-management",
    response_model=MediaManagementGetResponse,
    summary="Get Media Management Settings & Presets",
)
def get_media_management_settings(
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin),
) -> MediaManagementGetResponse:
    """Retrieves current media management settings and preset templates."""
    settings_dict = db.get_media_management_settings()
    return MediaManagementGetResponse(
        settings=MediaManagementSettingsModel(**settings_dict),
        presets=PRESETS,
        preset_descriptions=PRESET_DESCRIPTIONS,
        token_help=[
            {
                "group": g["group"],
                "tokens": [{"token": t, "description": d, "example": e} for t, d, e in g["tokens"]],
            }
            for g in TOKEN_HELP
        ],
        syntax_help=[{"syntax": a, "description": b, "example": c} for a, b, c in SYNTAX_HELP],
    )


@router.put(
    "/media-management",
    response_model=MediaManagementSettingsModel,
    summary="Update Media Management Settings (Admin Only, PUT alias)",
    include_in_schema=False,
)
@router.post(
    "/media-management",
    response_model=MediaManagementSettingsModel,
    summary="Update Media Management Settings (Admin Only)",
)
def update_media_management_settings(
    payload: MediaManagementUpdateModel,
    db: Database = Depends(get_db),
    admin_user: dict[str, Any] = Depends(require_admin),
) -> MediaManagementSettingsModel:
    """Admin-only: updates media management naming templates and options."""
    updates = payload.model_dump(exclude_unset=True)
    if "library_mode" in updates:
        # The mode is an interlock with in-flight checks: it can only change through PUT /library-manager.
        logger.warning("Ignoring library_mode in media-management update; use PUT /api/settings/library-manager")
        updates.pop("library_mode")
    if not updates:
        current = db.get_media_management_settings()
        return MediaManagementSettingsModel(**current)

    try:
        updated = db.update_media_management_settings(updates)
        return MediaManagementSettingsModel(**updated)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:
        logger.error("Failed to update media management settings: %s", redact_text(str(e)))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database update failed: {redact_text(str(e))}",
        ) from e


@router.post(
    "/media-management/preview",
    response_model=PreviewResponseModel,
    summary="Live Preview Token Templates (In-Memory)",
)
def preview_media_management_templates(
    payload: PreviewRequestModel | None = None,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin),
) -> PreviewResponseModel:
    """Renders real-time example paths purely in-memory using provided or stored settings."""
    stored_settings = db.get_media_management_settings()

    effective_settings = dict(stored_settings)
    if payload:
        overrides = payload.model_dump(exclude_unset=True)
        effective_settings.update(overrides)

    previews = _render_previews_for_settings(effective_settings)
    return PreviewResponseModel(
        previews=previews,
        format_previews=_render_format_previews(effective_settings),
    )


# -----------------------------------------------------------------------------
# Lidarr Automation Settings Endpoints
# -----------------------------------------------------------------------------


def _mask_lidarr_settings(settings: dict[str, Any]) -> dict[str, Any]:
    res = dict(settings)
    if res.get("api_key"):
        res["api_key"] = mask_secret(res["api_key"])
    return res


@router.get(
    "/lidarr",
    response_model=LidarrSettingsModel,
    summary="Get Lidarr Automation Settings",
)
def get_lidarr_settings(
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin),
) -> LidarrSettingsModel:
    """Retrieves Lidarr automation settings with masked API key."""
    settings = db.get_lidarr_settings()
    masked = _mask_lidarr_settings(settings)
    return LidarrSettingsModel(**masked)


@router.put(
    "/lidarr",
    response_model=LidarrSettingsModel,
    summary="Update Lidarr Automation Settings (Admin Only)",
    dependencies=[Depends(require_core_tier)],
)
@router.post(
    "/lidarr",
    response_model=LidarrSettingsModel,
    summary="Update Lidarr Automation Settings (Admin Only)",
    dependencies=[Depends(require_core_tier)],
)
def update_lidarr_settings(
    payload: LidarrSettingsUpdateModel,
    db: Database = Depends(get_db),
    admin_user: dict[str, Any] = Depends(require_admin),
) -> LidarrSettingsModel:
    """Admin-only: updates Lidarr automation settings in database.

    Preserves existing API key if masked or empty.
    """
    existing = db.get_lidarr_settings()
    updates = payload.model_dump(exclude_unset=True)

    if "url" in updates and updates["url"]:
        clean_url = str(updates["url"]).strip().rstrip("/")
        if not is_safe_service_url(clean_url):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Prohibited or invalid host URL (SSRF defense)",
            )
        updates["url"] = clean_url

    if "api_key" in updates:
        k = updates["api_key"]
        # If masked (contains * or •) or empty, keep existing
        if k and ("*" in k or "•" in k):
            updates["api_key"] = existing.get("api_key")
        elif not k:
            updates["api_key"] = existing.get("api_key")
        else:
            updates["api_key"] = k.strip()

    try:
        updated = db.update_lidarr_settings(updates)
        lidarr_library.invalidate()  # URL / key may have changed: never serve the old server's cached library
        invalidate_add_defaults()  # ...nor its cached root-folder defaults
        masked = _mask_lidarr_settings(updated)
        return LidarrSettingsModel(**masked)
    except Exception as e:
        logger.error("Failed to update Lidarr settings: %s", redact_text(str(e)))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database update failed: {redact_text(str(e))}",
        ) from e


@router.post(
    "/lidarr/test",
    response_model=LidarrTestConnectionResponse,
    summary="Test Lidarr Connection (Admin Only)",
)
def test_lidarr_connection(
    payload: LidarrTestConnectionPayload,
    db: Database = Depends(get_db),
    admin_user: dict[str, Any] = Depends(require_admin),
) -> LidarrTestConnectionResponse:
    """Admin-only: tests connectivity and credentials for Lidarr instance."""
    clean_url = str(payload.url).strip().rstrip("/")
    if not is_safe_service_url(clean_url):
        return LidarrTestConnectionResponse(
            online=False,
            version=None,
            error="Prohibited or invalid host URL (SSRF defense)",
        )

    api_key = payload.api_key.strip()
    if not api_key or "*" in api_key or "•" in api_key:
        existing = db.get_lidarr_settings()
        api_key = str(existing.get("api_key") or "")

    try:
        client = LidarrClient(base_url=clean_url, api_key=api_key)
        result = client.test_connection()
        return LidarrTestConnectionResponse(
            online=bool(result.get("online", False)),
            version=result.get("version"),
            error=redact_text(str(result["error"])) if result.get("error") else None,
        )
    except Exception as e:
        logger.warning("Lidarr connection test failed with exception: %s", redact_text(str(e)))
        return LidarrTestConnectionResponse(
            online=False,
            version=None,
            error=redact_text(str(e)),
        )


# -----------------------------------------------------------------------------
# General Application Settings Endpoints
# -----------------------------------------------------------------------------


@router.get(
    "/general",
    response_model=GeneralSettingsModel,
    summary="Get General Application Settings",
)
def get_general_settings(
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(require_admin),
) -> GeneralSettingsModel:
    """Retrieves general system settings including application URL."""
    settings = db.get_general_settings()
    return GeneralSettingsModel(**settings)


@router.post(
    "/general",
    response_model=GeneralSettingsModel,
    summary="Update General Application Settings (Admin Only)",
)
def update_general_settings(
    payload: GeneralSettingsUpdateModel,
    db: Database = Depends(get_db),
    admin_user: dict[str, Any] = Depends(require_admin),
) -> GeneralSettingsModel:
    """Admin-only: updates general system settings in database."""
    updates = payload.model_dump(exclude_unset=True)
    if "application_url" in updates and updates["application_url"] is not None:
        url = updates["application_url"].strip().rstrip("/")
        if url:
            if not (url.startswith("http://") or url.startswith("https://")):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Application URL must start with http:// or https://",
                )
        updates["application_url"] = url

    try:
        updated = db.update_general_settings(updates)
        return GeneralSettingsModel(**updated)
    except Exception as e:
        logger.error("Failed to update general settings: %s", redact_text(str(e)))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database update failed: {redact_text(str(e))}",
        ) from e


# -----------------------------------------------------------------------------
# API Key Settings Endpoints
# -----------------------------------------------------------------------------


class ApiKeyResponse(BaseModel):
    api_key: str


class ApiKeyRegenerateResponse(BaseModel):
    api_key: str
    message: str = "API key successfully regenerated"


@router.get(
    "/api-key",
    response_model=ApiKeyResponse,
    summary="Get API Key (Admin or API Key)",
)
def get_api_key(
    db: Database = Depends(get_db),
    admin_user: dict[str, Any] = Depends(require_admin),
) -> ApiKeyResponse:
    """Retrieves the machine API key."""
    return ApiKeyResponse(api_key=db.get_api_key())


@router.post(
    "/api-key/regenerate",
    response_model=ApiKeyRegenerateResponse,
    summary="Regenerate API Key (Admin Only)",
)
def regenerate_api_key(
    db: Database = Depends(get_db),
    admin_user: dict[str, Any] = Depends(require_admin),
) -> ApiKeyRegenerateResponse:
    """Regenerates the machine API key."""
    new_key = db.regenerate_api_key()
    return ApiKeyRegenerateResponse(
        api_key=new_key,
        message="API key successfully regenerated",
    )



# -----------------------------------------------------------------------------
# Library manager (TrackSeerr vs Lidarr interlock)
# -----------------------------------------------------------------------------


class LibraryManagerModel(BaseModel):
    mode: Literal["native", "lidarr"]
    lidarr_configured: bool
    native_configured: bool
    can_switch: bool
    blocking_reason: str | None = None


class LibraryManagerUpdateModel(BaseModel):
    mode: Literal["native", "lidarr"]


@router.get(
    "/library-manager",
    response_model=LibraryManagerModel,
    summary="Get Library Manager Mode (Admin Only)",
    dependencies=[Depends(require_core_tier)],
)
def get_library_manager(
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Which side manages the library, whether each side is usable, and whether a switch is allowed right now."""
    return library_manager.get_status(db, config)


@router.put(
    "/library-manager",
    response_model=LibraryManagerModel,
    summary="Switch Library Manager Mode (Admin Only)",
    dependencies=[Depends(require_core_tier)],
    responses={409: {"description": "Work is in flight"}, 422: {"description": "Lidarr is not configured"}},
)
def set_library_manager(
    payload: LibraryManagerUpdateModel,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    admin_user: dict[str, Any] = Depends(require_admin),
) -> Any:
    """Switches the library manager. Refused (409) while downloads or a Lidarr trickle are in flight, and (422)
    when switching to Lidarr before it is configured. The inactive side's settings are preserved untouched."""
    current = library_manager.get_library_mode(db)
    if payload.mode == current:
        return library_manager.get_status(db, config)

    if payload.mode == library_manager.MODE_LIDARR and not library_manager.lidarr_is_configured(db, config):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Configure Lidarr (URL and API key) before switching the library manager to Lidarr.",
        )

    try:
        library_manager.switch_mode(db, payload.mode, source="Settings", user=admin_user.get("username"))
    except library_manager.SwitchRefused as refused:
        body = library_manager.get_status(db, config)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={**body, "can_switch": False, "blocking_reason": refused.reason, "detail": refused.reason},
        )
    except sqlite3.Error as exc:
        logger.error("Failed to switch library manager: %s", redact_text(str(exc)))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not save the library manager mode"
        ) from exc
    return library_manager.get_status(db, config)


@router.get(
    "/lidarr/defaults",
    response_model=LidarrDefaultsResponse,
    summary="Lidarr Root-Folder Defaults Trackseerr Adds Artists With (Admin Only)",
    dependencies=[Depends(require_core_tier)],
)
def get_lidarr_defaults(
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    _admin: dict[str, Any] = Depends(require_admin),
) -> LidarrDefaultsResponse:
    """The read-only defaults (profiles, monitoring, tags) Trackseerr uses, straight from Lidarr's root folders."""
    client = library_manager.build_lidarr_client(db, config)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Lidarr is not configured (URL and API key are required).",
        )
    try:
        defaults = client.get_root_folder_defaults()
        options = client.get_options()
        singles_enabled = client.metadata_profile_allows_singles(defaults.metadata_profile_id)
    except LidarrApiError as exc:
        logger.warning("Lidarr defaults request failed: %s", redact_text(str(exc)))
        message = redact_text(str(exc)).replace(client.api_key, "REDACTED") if client.api_key else redact_text(str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Lidarr request failed: {message}") from exc
    quality_names = {int(q["id"]): str(q["name"]) for q in options["quality_profiles"]}
    metadata_names = {int(m["id"]): str(m["name"]) for m in options["metadata_profiles"]}
    tag_labels = {int(t["id"]): str(t["label"]) for t in options["tags"]}
    return LidarrDefaultsResponse(
        root_folder=defaults.root_folder_path,
        quality_profile=LidarrNamedProfile(
            id=defaults.quality_profile_id,
            name=quality_names.get(defaults.quality_profile_id, f"Profile {defaults.quality_profile_id}"),
        ),
        metadata_profile=LidarrNamedProfile(
            id=defaults.metadata_profile_id,
            name=metadata_names.get(defaults.metadata_profile_id, f"Profile {defaults.metadata_profile_id}"),
        ),
        monitor=defaults.monitor,
        new_item_monitor=defaults.new_item_monitor,
        tags=[LidarrTagModel(id=t, label=tag_labels.get(t, f"Tag {t}")) for t in defaults.tag_ids],
        source="rootfolder" if defaults.source == "rootfolder" else "fallback",
        root_folders=[r["path"] for r in options["root_folders"]],
        singles_enabled=singles_enabled,
    )


@router.get(
    "/lidarr/options",
    summary="Live Lidarr Root Folders, Profiles and Tags (Admin Only)",
    dependencies=[Depends(require_core_tier)],
)
def get_lidarr_options(
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Fetches the pickers for the Lidarr settings form straight from Lidarr; 502 with a redacted message on failure."""
    client = library_manager.build_lidarr_client(db, config)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Lidarr is not configured (URL and API key are required).",
        )
    try:
        return client.get_options()
    except LidarrApiError as exc:
        logger.warning("Lidarr options request failed: %s", redact_text(str(exc)))
        message = redact_text(str(exc)).replace(client.api_key, "REDACTED") if client.api_key else redact_text(str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Lidarr request failed: {message}") from exc
