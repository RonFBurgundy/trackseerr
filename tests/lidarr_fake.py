"""A small in-memory Lidarr v1 API for tests: patch ``httpx.Client`` in the client module with an instance of it.

Every request is recorded in ``calls`` as ``(METHOD, path, json_body)`` where ``path`` is the part after
``/api/v1/`` including the query string.
"""

import copy
import time as _real_time
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

import httpx

class FastClock:
    """Stands in for the ``time`` name inside one module (``patch("pkg.mod.time", FastClock())``).

    ``sleep`` advances the clock instead of waiting, so a cooldown loop (``while time.time() < until: time.sleep(1)``)
    finishes after a few iterations. Patching ``pkg.mod.time.sleep`` instead replaces the process-global
    ``time.sleep`` with a no-op mock, and such a loop then spins on the real clock for the whole cooldown while the
    mock records every call (hundreds of MB per test).
    """

    def __init__(self) -> None:
        self.offset = 0.0

    def time(self) -> float:
        return _real_time.time() + self.offset

    def monotonic(self) -> float:
        return _real_time.monotonic() + self.offset

    def sleep(self, seconds: float) -> None:
        self.offset += seconds


ROOT_DEFAULTS: dict[str, Any] = {
    "path": "/music",
    "defaultQualityProfileId": 4,
    "defaultMetadataProfileId": 6,
    "defaultMonitorOption": "future",
    "defaultNewItemMonitorOption": "new",
    "defaultTags": [7],
}


PRIMARY_ALBUM_TYPES = ("Album", "EP", "Single", "Broadcast", "Other")


def metadata_profile(profile_id: int, name: str, singles: bool = True) -> dict[str, Any]:
    """A Lidarr MetadataProfileResource: ``primaryAlbumTypes`` is a list of ``{albumType: {id, name}, allowed}``
    (ProfilePrimaryAlbumTypeItemResource), mirroring Lidarr's ``GET /api/v1/metadataprofile``."""
    return {
        "id": profile_id,
        "name": name,
        "primaryAlbumTypes": [
            {"albumType": {"id": idx, "name": kind}, "allowed": singles or kind != "Single"}
            for idx, kind in enumerate(PRIMARY_ALBUM_TYPES)
        ],
        "secondaryAlbumTypes": [],
        "releaseStatuses": [],
    }


class FakeLidarr:
    NEW_ARTIST_ID = 99

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.root_folders: list[dict[str, Any]] = [dict(ROOT_DEFAULTS)]
        self.quality_profiles = [{"id": 1, "name": "Any"}, {"id": 4, "name": "Lossless"}]
        self.metadata_profiles = [metadata_profile(1, "Standard"), metadata_profile(6, "Everything")]
        self.tags = [{"id": 7, "label": "family"}]
        # A real lookup row for an artist not yet in Lidarr carries no "id" key (the client treats that as new).
        self.lookup: list[dict[str, Any]] = [{"artistName": "Queen", "foreignArtistId": "mb-queen"}]
        self.albums: list[dict[str, Any]] = []  # what Lidarr lists for the artist once loaded
        self.tracks: list[dict[str, Any]] = []
        self.empty_album_polls = 0  # album?artistId= answers [] this many times first (async load after an add)
        self.monitor_sticks = True  # False: PUT album/monitor is accepted but the album stays unmonitored
        self.fail: dict[tuple[str, str], int] = {}  # (METHOD, path prefix) -> HTTP status to answer
        self.track_by_artist_supported = True
        # Lidarr queues a RefreshArtist command when an artist is added; it stays "started" for this many
        # ``GET /command`` polls and then reads "completed".
        self.refresh_polls = 0
        self.commands: list[dict[str, Any]] = []
        self.command_list_supported = True  # False: GET /command answers 404 (the client must fall back to counts)
        # Successive snapshots of the artist's albums Lidarr serves while it loads them (the last one repeats).
        # Real Lidarr (3.1.0.4875): after POST artist with addOptions.monitor "none", the artist's albums list as
        # monitored=true and GET artist/{id} carries addOptions until the refresh has completed AND one more artist
        # read has happened; only then are the albums unmonitored and addOptions null. Opt-in (existing tests predate it).
        self.model_add_window = False
        self.add_window_open = False
        self.add_window_extra_polls = 1
        self.add_options: Optional[dict[str, Any]] = None
        self.new_artist_profile_id: Optional[int] = None
        self.album_snapshots: Optional[list[list[dict[str, Any]]]] = None
        self._visible_album_ids: Optional[set[int]] = None

    # httpx.Client stand-in -------------------------------------------------------------------------------------
    def __call__(self, *args: Any, **kwargs: Any) -> "FakeLidarr":
        return self

    def __enter__(self) -> "FakeLidarr":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    # helpers --------------------------------------------------------------------------------------------------
    def requests(self, method: str, prefix: str) -> list[Any]:
        return [body for m, p, body in self.calls if m == method and p.startswith(prefix)]

    def paths(self, method: str) -> list[str]:
        return [p for m, p, _ in self.calls if m == method]

    def _close_add_window(self) -> None:
        """Lidarr applies addOptions.monitor: "none" unmonitors every album and clears addOptions."""
        self.add_window_open = False
        self.add_options = None
        for album in self.albums:
            album["monitored"] = False

    def _album(self, album_id: int) -> Optional[dict[str, Any]]:
        return next((a for a in self.albums if a.get("id") == album_id), None)

    def _respond(self, method: str, url: str, body: Any = None) -> httpx.Response:
        parts = urlsplit(url)
        path = parts.path.split("/api/v1/", 1)[1]
        full = path + (f"?{parts.query}" if parts.query else "")
        query = parse_qs(parts.query)
        self.calls.append((method, full, copy.deepcopy(body)))
        for (m, prefix), code in self.fail.items():
            if m == method and full.startswith(prefix):
                return httpx.Response(code, json={"message": "boom"}, headers={"Retry-After": "7"})

        if method == "GET":
            if path == "rootfolder":
                return httpx.Response(200, json=self.root_folders)
            if path == "qualityprofile":
                return httpx.Response(200, json=self.quality_profiles)
            if path == "metadataprofile":
                return httpx.Response(200, json=self.metadata_profiles)
            if path.startswith("metadataprofile/"):
                profile = next((p for p in self.metadata_profiles if p["id"] == int(path.split("/")[1])), None)
                return httpx.Response(200 if profile else 404, json=copy.deepcopy(profile) if profile else {})
            if path == "tag":
                return httpx.Response(200, json=self.tags)
            if path == "artist/lookup":
                return httpx.Response(200, json=self.lookup)
            if path.startswith("artist/") and path.split("/")[1].isdigit():
                if int(path.split("/")[1]) != self.NEW_ARTIST_ID or not self.model_add_window:
                    return httpx.Response(404, json={})
                if self.add_window_open and not any(c["status"] in ("queued", "started") for c in self.commands):
                    if self.add_window_extra_polls > 0:
                        self.add_window_extra_polls -= 1
                    else:
                        self._close_add_window()
                return httpx.Response(
                    200,
                    json={"id": self.NEW_ARTIST_ID, "metadataProfileId": self.new_artist_profile_id,
                          "addOptions": copy.deepcopy(self.add_options)},
                )
            if path == "album" and "artistId" in query:
                if self.empty_album_polls > 0:
                    self.empty_album_polls -= 1
                    return httpx.Response(200, json=[])
                if self.album_snapshots:
                    snapshot = self.album_snapshots.pop(0) if len(self.album_snapshots) > 1 else self.album_snapshots[0]
                    self._visible_album_ids = {int(a["id"]) for a in snapshot}
                    return httpx.Response(200, json=copy.deepcopy(snapshot))
                if self.add_window_open:
                    return httpx.Response(200, json=[{**copy.deepcopy(a), "monitored": True} for a in self.albums])
                return httpx.Response(200, json=copy.deepcopy(self.albums))
            if path == "command":
                if not self.command_list_supported:
                    return httpx.Response(404, json={})
                for cmd in self.commands:
                    if cmd["status"] in ("queued", "started"):
                        if self.refresh_polls > 0:
                            self.refresh_polls -= 1
                            cmd["status"] = "started"
                        if self.refresh_polls == 0:
                            cmd["status"] = "completed"
                return httpx.Response(200, json=copy.deepcopy(self.commands))
            if path.startswith("album/"):
                album = self._album(int(path.split("/")[1]))
                if album is not None and self.add_window_open:
                    album = {**album, "monitored": True}
                return httpx.Response(200 if album else 404, json=copy.deepcopy(album) if album else {})
            if path == "track":
                if "artistId" in query:
                    rows = [
                        t
                        for t in self.tracks
                        if self._visible_album_ids is None or t.get("albumId") in self._visible_album_ids
                    ]
                    return httpx.Response(200, json=copy.deepcopy(rows) if self.track_by_artist_supported else [])
                if "albumId" in query:
                    wanted = int(query["albumId"][0])
                    return httpx.Response(200, json=[t for t in self.tracks if t.get("albumId") == wanted])
        if method == "POST":
            if path == "artist":
                # Lidarr v1: a RefreshArtist command resource; ``body.artistIds`` carries the id (``artistId`` is
                # JsonIgnore'd server side), ``status`` is a lowercase CommandStatus.
                self.commands.append(
                    {
                        "id": 100 + len(self.commands),
                        "name": "RefreshArtist",
                        "commandName": "Refresh Artist",
                        "status": "queued" if self.refresh_polls > 0 else "completed",
                        "body": {"artistIds": [self.NEW_ARTIST_ID], "isNewArtist": True, "name": "RefreshArtist"},
                    }
                )
                if self.model_add_window:
                    self.add_window_open = True
                    self.add_options = copy.deepcopy(body.get("addOptions"))
                    self.new_artist_profile_id = body.get("metadataProfileId")
                return httpx.Response(201, json={**body, "id": self.NEW_ARTIST_ID})
            if path == "command":
                return httpx.Response(201, json={"id": 1, "name": body.get("name")})
        if method == "PUT":
            if path == "album/monitor":
                if self.monitor_sticks:
                    for album_id in body["albumIds"]:
                        album = self._album(album_id)
                        if album is not None:
                            album["monitored"] = body["monitored"]
                return httpx.Response(202, json=[])
        return httpx.Response(404, json={"message": f"unhandled {method} {path}"})

    def get(self, url: str, headers: Any = None, params: Any = None) -> httpx.Response:
        if params:
            url = f"{url}?" + "&".join(f"{k}={v}" for k, v in params.items())
        return self._respond("GET", url)

    def post(self, url: str, headers: Any = None, json: Any = None) -> httpx.Response:
        return self._respond("POST", url, json)

    def put(self, url: str, headers: Any = None, json: Any = None) -> httpx.Response:
        return self._respond("PUT", url, json)

    def delete(self, url: str, headers: Any = None) -> httpx.Response:
        return self._respond("DELETE", url)
