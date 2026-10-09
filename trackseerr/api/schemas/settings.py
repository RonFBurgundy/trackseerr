"""Response models for the route of ``api/routes/settings.py`` that had no model (``/lidarr/options``).

Every other settings route already has a router-level model; the ones carrying secrets (Lidarr key, AcoustID key,
media-server credentials) mask them before building the model, and are left untouched. ``GET /api-key`` returns the
real API key to an admin by design.
"""

from trackseerr.api.response_models import ApiModel


class LidarrRootFolder(ApiModel):
    path: str
    free_space: int


class LidarrNamedOption(ApiModel):
    id: int
    name: str


class LidarrTagOption(ApiModel):
    id: int
    label: str


class LidarrOptionsResponse(ApiModel):
    root_folders: list[LidarrRootFolder]
    quality_profiles: list[LidarrNamedOption]
    metadata_profiles: list[LidarrNamedOption]
    tags: list[LidarrTagOption]
