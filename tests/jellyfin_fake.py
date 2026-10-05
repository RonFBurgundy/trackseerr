"""In-process fake Jellyfin server (httpx MockTransport) for the adapter and contract tests.

Modelled on the Jellyfin 10.10 REST API and checked against a real server by
``tests/integration/test_jellyfin_contract.py``. It verifies the ``Authorization: MediaBrowser ... Token="..."`` header
(and rejects a key passed in the URL), requires a ``UserId`` on every playlist call (an API key has no user context),
pages ``/Items`` and ``/Playlists/{id}/Items`` with ``StartIndex`` / ``Limit`` / ``TotalRecordCount``, matches
``SearchTerm`` against the item name only, gives every playlist entry its own ``PlaylistItemId``, removes by
``EntryIds`` and answers ``Move/{newIndex}`` with 400 like a real server does for an API key (it needs a user session).
"""

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl

import httpx

API_KEY = "test-api-key"


@dataclass
class FakeAudio:
    id: str
    title: str
    artist: str
    album: str
    seconds: int = 200


@dataclass
class FakeEntry:
    entry_id: str
    item_id: str


@dataclass
class FakePlaylist:
    id: str
    name: str
    owner_id: str
    public: bool = True  # Jellyfin creates playlists public unless the request says IsPublic=false
    entries: list[FakeEntry] = field(default_factory=list)


@dataclass
class FakeUser:
    id: str
    name: str
    admin: bool = False


@dataclass
class JellyfinState:
    songs: list[FakeAudio] = field(default_factory=list)
    playlists: dict[str, FakePlaylist] = field(default_factory=dict)
    users: list[FakeUser] = field(default_factory=list)
    api_keys: set[str] = field(default_factory=lambda: {API_KEY})
    scans: int = 0
    next_playlist: int = 1
    next_entry: int = 1
    max_page: int = 100  # server-side cap on Limit, to prove the adapter pages
    calls: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)  # (method, path, query)
    headers_seen: list[str] = field(default_factory=list)
    urls_seen: list[str] = field(default_factory=list)
    # Test hooks: ``transport_failure`` is raised instead of answering; ``respond_with`` may return a raw response
    # for (method, path, call number) (returning None falls through).
    transport_failure: Optional[Callable[[], Exception]] = None
    respond_with: Optional[Callable[[str, str, int], Optional[httpx.Response]]] = None
    request_counts: dict[str, int] = field(default_factory=dict)

    def song(self, item_id: str) -> Optional[FakeAudio]:
        return next((s for s in self.songs if s.id == item_id), None)

    def user(self, user_id: str) -> Optional[FakeUser]:
        return next((u for u in self.users if u.id == user_id), None)


def _clean(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text.lower())


class FakeJellyfin:
    def __init__(self, state: Optional[JellyfinState] = None) -> None:
        self.state = state or JellyfinState()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ---------------------------------------------------------------- plumbing

    @staticmethod
    def _json(body: Any, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=body)

    def handle(self, request: httpx.Request) -> httpx.Response:
        st = self.state
        if st.transport_failure is not None:
            raise st.transport_failure()
        method, path = request.method, request.url.path
        query = dict(parse_qsl(request.url.query.decode(), keep_blank_values=True))
        st.urls_seen.append(str(request.url))
        st.request_counts[f"{method} {path}"] = st.request_counts.get(f"{method} {path}", 0) + 1
        if st.respond_with is not None:
            raw = st.respond_with(method, path, st.request_counts[f"{method} {path}"])
            if raw is not None:
                return raw
        auth = request.headers.get("Authorization", "")
        st.headers_seen.append(auth)
        match = re.search(r'Token="([^"]*)"', auth)
        if not auth.startswith("MediaBrowser ") or match is None or match.group(1) not in st.api_keys:
            return httpx.Response(401)
        st.calls.append((method, path, query))
        for route_method, pattern, handler in self._routes():
            m = pattern.fullmatch(path)
            if m and route_method == method:
                return handler(request, query, *m.groups())
        return httpx.Response(404)

    def _routes(self) -> list[tuple[str, "re.Pattern[str]", Callable[..., httpx.Response]]]:
        def route(method: str, pattern: str, handler: Callable[..., httpx.Response]) -> tuple[str, "re.Pattern[str]", Callable[..., httpx.Response]]:
            return method, re.compile(pattern), handler

        return [
            route("GET", r"/System/Info", self._system_info),
            route("GET", r"/Users", self._users),
            route("GET", r"/Items", self._items),
            route("POST", r"/Library/Refresh", self._refresh),
            route("POST", r"/Playlists", self._create_playlist),
            route("GET", r"/Playlists/([^/]+)/Items", self._playlist_items),
            route("POST", r"/Playlists/([^/]+)/Items", self._add_items),
            route("DELETE", r"/Playlists/([^/]+)/Items", self._remove_items),
            route("POST", r"/Playlists/([^/]+)/Items/([^/]+)/Move/(\d+)", self._move),
        ]

    @staticmethod
    def _page(items: list[dict[str, Any]], query: dict[str, str], cap: int) -> dict[str, Any]:
        start = int(query.get("StartIndex", "0"))
        limit = min(int(query.get("Limit", str(cap))), cap)
        return {"Items": items[start : start + limit], "TotalRecordCount": len(items), "StartIndex": start}

    # ---------------------------------------------------------------- endpoints

    def _system_info(self, request: httpx.Request, query: dict[str, str]) -> httpx.Response:
        return self._json({"ServerName": "fake-jellyfin", "Version": "10.10.0", "Id": "srv"})

    def _users(self, request: httpx.Request, query: dict[str, str]) -> httpx.Response:
        return self._json([{"Id": u.id, "Name": u.name, "Policy": {"IsAdministrator": u.admin}} for u in self.state.users])

    @staticmethod
    def _audio_json(s: FakeAudio) -> dict[str, Any]:
        return {
            "Id": s.id,
            "Name": s.title,
            "Type": "Audio",
            "Album": s.album,
            "AlbumArtist": s.artist,
            "Artists": [s.artist],
            "ArtistItems": [{"Id": f"a-{s.artist}", "Name": s.artist}],
            "RunTimeTicks": s.seconds * 10_000_000,
            "MediaType": "Audio",
        }

    def _items(self, request: httpx.Request, query: dict[str, str]) -> httpx.Response:
        st = self.state
        kinds = query.get("IncludeItemTypes", "")
        if kinds == "Playlist":
            user = st.user(query.get("UserId", ""))
            if user is None:
                return httpx.Response(400)
            rows = [
                {"Id": p.id, "Name": p.name, "Type": "Playlist", "MediaType": "Audio", "ChildCount": len(p.entries)}
                for p in st.playlists.values()
                if p.owner_id == user.id or p.public
            ]
            return self._json(self._page(rows, query, st.max_page))
        if kinds != "Audio":
            return httpx.Response(400)
        songs = st.songs
        if "Ids" in query:
            wanted = query["Ids"].split(",")
            songs = [s for s in songs if s.id in wanted]
        if "SearchTerm" in query:
            term = _clean(query["SearchTerm"]).strip()
            songs = [s for s in songs if term in _clean(s.title)]  # name only, like Jellyfin
        return self._json(self._page([self._audio_json(s) for s in songs], query, st.max_page))

    def _refresh(self, request: httpx.Request, query: dict[str, str]) -> httpx.Response:
        self.state.scans += 1
        return httpx.Response(204)

    def _owned(self, playlist_id: str, user_id: str) -> Optional[FakePlaylist]:
        playlist = self.state.playlists.get(playlist_id)
        return playlist if playlist is not None and (playlist.owner_id == user_id or playlist.public) else None

    def _new_entry(self, item_id: str) -> FakeEntry:
        entry = FakeEntry(f"e{self.state.next_entry}", item_id)
        self.state.next_entry += 1
        return entry

    def _create_playlist(self, request: httpx.Request, query: dict[str, str]) -> httpx.Response:
        import json

        st = self.state
        body = json.loads(request.content or b"{}")
        user = st.user(str(body.get("UserId") or ""))
        if user is None or not body.get("Name") or body.get("MediaType") != "Audio":
            return httpx.Response(400)
        ids = [str(i) for i in body.get("Ids") or []]
        if any(st.song(i) is None for i in ids):
            return httpx.Response(400)
        playlist = FakePlaylist(f"pl{st.next_playlist}", str(body["Name"]), user.id, public=bool(body.get("IsPublic", True)))
        st.next_playlist += 1
        playlist.entries = [self._new_entry(i) for i in ids]
        st.playlists[playlist.id] = playlist
        return self._json({"Id": playlist.id})

    def _playlist_items(self, request: httpx.Request, query: dict[str, str], playlist_id: str) -> httpx.Response:
        playlist = self._owned(playlist_id, query.get("UserId", ""))
        if playlist is None:
            return httpx.Response(404)
        rows = []
        for entry in playlist.entries:
            song = self.state.song(entry.item_id)
            if song is not None:
                rows.append({**self._audio_json(song), "PlaylistItemId": entry.entry_id})
        return self._json(self._page(rows, query, self.state.max_page))

    def _add_items(self, request: httpx.Request, query: dict[str, str], playlist_id: str) -> httpx.Response:
        playlist = self._owned(playlist_id, query.get("UserId", ""))
        if playlist is None:
            return httpx.Response(404)
        ids = [i for i in query.get("Ids", "").split(",") if i]
        if any(self.state.song(i) is None for i in ids):
            return httpx.Response(400)
        playlist.entries.extend(self._new_entry(i) for i in ids)
        return httpx.Response(204)

    def _remove_items(self, request: httpx.Request, query: dict[str, str], playlist_id: str) -> httpx.Response:
        playlist = self.state.playlists.get(playlist_id)
        if playlist is None:
            return httpx.Response(404)
        drop = set(query.get("EntryIds", "").split(","))
        playlist.entries = [e for e in playlist.entries if e.entry_id not in drop]
        return httpx.Response(204)

    def _move(self, request: httpx.Request, query: dict[str, str], playlist_id: str, entry_id: str, new_index: str) -> httpx.Response:
        return httpx.Response(400, text="Error processing request.")  # real Jellyfin 12.1: API keys have no user for Move


def default_state(songs: list[tuple[str, str, str]]) -> JellyfinState:
    """State with an admin ``admin`` (id ``u-admin``) and a regular user ``kid`` (id ``u-kid``), one song per ``(title, artist, album)``."""
    return JellyfinState(
        songs=[FakeAudio(id=f"s{i}", title=t, artist=a, album=al) for i, (t, a, al) in enumerate(songs, start=1)],
        users=[FakeUser("u-admin", "admin", True), FakeUser("u-kid", "kid", False)],
    )
