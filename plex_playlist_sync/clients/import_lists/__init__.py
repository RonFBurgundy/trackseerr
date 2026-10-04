"""Import list providers: each module fetches a remote list into ``ImportListItem`` rows.

``PROVIDERS`` holds the metadata the UI renders its forms from, so a new provider is one module plus one entry.
"""

from typing import Any, Callable, Optional

from plex_playlist_sync.clients.import_lists import lastfm, listenbrainz, musicbrainz_collection
from plex_playlist_sync.clients.import_lists.base import ImportListError, ImportListItem

SECRET_PLACEHOLDER = "********"

_FetchFn = Callable[..., list[ImportListItem]]

PROVIDERS: dict[str, dict[str, Any]] = {
    "lastfm": {
        "label": "Last.fm",
        "fetch": lastfm.fetch,
        "sources": list(lastfm.SOURCES),
        "fields": [
            {"key": "username", "label": "Username", "type": "text", "required": True},
            {"key": "api_key", "label": "API key (blank uses LASTFM_API_KEY)", "type": "secret", "required": False},
            {"key": "source", "label": "List", "type": "select", "required": True, "options": list(lastfm.SOURCES)},
            {"key": "period", "label": "Period", "type": "select", "required": False, "options": list(lastfm.PERIODS)},
            {"key": "limit", "label": "Max items", "type": "number", "required": False},
        ],
    },
    "listenbrainz": {
        "label": "ListenBrainz",
        "fetch": listenbrainz.fetch,
        "sources": list(listenbrainz.SOURCES),
        "fields": [
            {"key": "username", "label": "Username", "type": "text", "required": False},
            {"key": "token", "label": "User token (optional)", "type": "secret", "required": False},
            {"key": "source", "label": "List", "type": "select", "required": True, "options": list(listenbrainz.SOURCES)},
            {"key": "playlist_mbid", "label": "Playlist MBID (for playlist)", "type": "text", "required": False},
            {"key": "range", "label": "Range (for stats)", "type": "select", "required": False, "options": list(listenbrainz.RANGES)},
            {"key": "limit", "label": "Max items (for stats)", "type": "number", "required": False},
        ],
    },
    "musicbrainz_collection": {
        "label": "MusicBrainz collection",
        "fetch": musicbrainz_collection.fetch,
        "sources": ["artists", "release_groups", "releases"],
        "fields": [
            {"key": "collection_mbid", "label": "Collection MBID", "type": "text", "required": True},
        ],
    },
}

# Config keys holding secrets, per provider (derived from the field metadata).
SECRET_KEYS: dict[str, tuple[str, ...]] = {
    name: tuple(f["key"] for f in meta["fields"] if f["type"] == "secret") for name, meta in PROVIDERS.items()
}


def provider_metadata() -> list[dict[str, Any]]:
    """The provider list for ``GET /api/import-lists/providers`` (no callables)."""
    return [
        {"provider": name, "label": meta["label"], "sources": list(meta["sources"]), "fields": [dict(f) for f in meta["fields"]]}
        for name, meta in PROVIDERS.items()
    ]


def fetch_items(provider: str, config: dict[str, Any], mb_base_url: Optional[str] = None) -> list[ImportListItem]:
    """Fetches every item of a list. Raises ImportListError for an unknown provider or a failed/invalid fetch."""
    meta = PROVIDERS.get(provider)
    if meta is None:
        raise ImportListError(f"Unknown import list provider {provider!r}")
    fetch: _FetchFn = meta["fetch"]
    return fetch(config, mb_base_url=mb_base_url)


__all__ = [
    "ImportListError",
    "ImportListItem",
    "PROVIDERS",
    "SECRET_KEYS",
    "SECRET_PLACEHOLDER",
    "fetch_items",
    "provider_metadata",
]
