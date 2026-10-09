"""Response models for ``api/routes/quality_catalog.py`` (only the routes the router did not already type)."""

from typing import Any, Optional

from trackseerr.api.response_models import ApiModel


class ImportedCustomFormat(ApiModel):
    """A stored custom format plus the import outcome (``created`` or ``updated``)."""

    id: int
    name: str
    include_in_rename: bool = False
    # Free-form: each specification's ``fields`` depends on its (possibly unknown) Lidarr implementation.
    specifications: list[dict[str, Any]]
    unsupported: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    action: str


class CustomFormatImportError(ApiModel):
    index: int
    name: Optional[str] = None
    errors: list[str]


class CustomFormatImportResponse(ApiModel):
    imported: list[ImportedCustomFormat]
    errors: list[CustomFormatImportError]


class ExportedSpecification(ApiModel):
    name: str
    implementation: str
    negate: bool
    required: bool
    # Free-form: Lidarr's per-implementation field object.
    fields: dict[str, Any]


class ExportedCustomFormat(ApiModel):
    """Lidarr/Servarr-schema JSON (camelCase key is the external schema)."""

    name: str
    includeCustomFormatWhenRenaming: bool
    specifications: list[ExportedSpecification]


class FormatDeletedResponse(ApiModel):
    status: str
    id: int
