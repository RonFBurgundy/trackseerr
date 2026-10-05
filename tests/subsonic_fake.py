"""In-process fake Subsonic server (httpx MockTransport) for the adapter and contract tests.

Written from the Subsonic 1.16.1 / OpenSubsonic API docs and checked against a real Navidrome by
``tests/integration/test_navidrome_contract.py``. It implements the endpoints Trackseerr uses faithfully enough to
catch protocol mistakes: token auth is verified (``t == md5(password + s)``), repeated ``songId`` /
``songIdToAdd`` / ``songIndexToRemove`` parameters are honoured, ``updatePlaylist`` removes by index (all indexes
refer to the playlist before the call) then appends, and errors use the ``subsonic-response`` failure envelope.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl

import httpx

OK = "ok"
FAILED = "failed"

ERR_GENERIC = 0
ERR_WRONG_CREDENTIALS = 40
ERR_NOT_AUTHORIZED = 50
ERR_NOT_FOUND = 70


class Fault(Exception):
    """Raised inside a handler to answer with a Subsonic failure envelope."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class FakeSong:
    id: str
    title: str
    artist: str
    album: str
    duration: int = 200


@dataclass
class FakePlaylist:
    id: str
    name: str
    owner: str
    entries: list[str] = field(default_factory=list)
    comment: str = ""


@dataclass
class FakeUser:
    username: str
    admin: bool = False
    password: str = ""


@dataclass
class SubsonicState:
    songs: list[FakeSong] = field(default_factory=list)
    playlists: dict[str, FakePlaylist] = field(default_factory=dict)
    users: dict[str, FakeUser] = field(default_factory=dict)
    api_keys: dict[str, str] = field(default_factory=dict)  # api key -> username
    advertise_api_key: bool = True
    scans: int = 0
    next_playlist_id: int = 1
    calls: list[tuple[str, list[tuple[str, str]]]] = field(default_factory=list)  # (endpoint, params without auth)
    # Test hooks. ``transport_failure`` is raised instead of answering; ``respond_with`` may return a raw
    # ``httpx.Response`` for an endpoint to simulate proxies / rate limits (returning None falls through).
    transport_failure: Optional[Callable[[], Exception]] = None
    fault: Optional[Callable[[], Fault]] = None
    respond_with: Optional[Callable[[str, int], Optional[httpx.Response]]] = None
    request_counts: dict[str, int] = field(default_factory=dict)
    # Servers differ on ``songIndexToRemove``: the spec says every index refers to the playlist before the call, but some
    # implementations remove one index at a time from the live list. Setting this models the latter.
    sequential_removal: bool = False

    def song(self, song_id: str) -> Optional[FakeSong]:
        return next((s for s in self.songs if s.id == song_id), None)


_AUTH_KEYS = {"u", "t", "s", "p", "apiKey"}


class FakeSubsonic:
    """``httpx.MockTransport``-compatible callable over a :class:`SubsonicState`."""

    def __init__(self, state: Optional[SubsonicState] = None) -> None:
        self.state = state or SubsonicState()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ---------------------------------------------------------------- plumbing

    @staticmethod
    def _envelope(status: str, **payload: Any) -> httpx.Response:
        body = {"subsonic-response": {"status": status, "version": "1.16.1", "type": "fake", "serverVersion": "1.0", **payload}}
        return httpx.Response(200, json=body)

    def handle(self, request: httpx.Request) -> httpx.Response:
        st = self.state
        if st.transport_failure is not None:
            raise st.transport_failure()
        path = request.url.path
        assert path.startswith("/rest/"), f"unexpected path {path}"
        endpoint = path[len("/rest/") :].removesuffix(".view")
        params = parse_qsl(request.url.query.decode(), keep_blank_values=True)
        st.request_counts[endpoint] = st.request_counts.get(endpoint, 0) + 1
        if st.respond_with is not None:
            raw = st.respond_with(endpoint, st.request_counts[endpoint])
            if raw is not None:
                return raw
        try:
            if st.fault is not None:
                raise st.fault()
            if dict(params).get("f") != "json":
                raise Fault(ERR_GENERIC, "this fake only speaks f=json")
            if dict(params).get("v") != "1.16.1":
                raise Fault(30, "Incompatible Subsonic REST protocol version. Server must upgrade.")
            if endpoint == "getOpenSubsonicExtensions":
                exts = [{"name": "apiKeyAuthentication", "versions": [1]}] if st.advertise_api_key else []
                return self._envelope(OK, openSubsonicExtensions=exts)
            user = self._authenticate(params)
            st.calls.append((endpoint, [(k, v) for k, v in params if k not in _AUTH_KEYS]))
            handler = getattr(self, f"_ep_{endpoint}", None)
            if handler is None:
                raise Fault(ERR_GENERIC, f"unknown endpoint {endpoint}")
            return self._envelope(OK, **handler(user, params))
        except Fault as fault:
            return self._envelope(FAILED, error={"code": fault.code, "message": fault.message})

    def _authenticate(self, params: list[tuple[str, str]]) -> FakeUser:
        p = dict(params)
        if "apiKey" in p:
            if "u" in p or "t" in p:
                raise Fault(43, "Multiple conflicting authentication mechanisms provided")
            name = self.state.api_keys.get(p["apiKey"])
            if name is None:
                raise Fault(44, "Invalid API key")
            return self.state.users[name]
        if "p" in p:
            raise Fault(42, "Provided authentication mechanism not supported")
        user = self.state.users.get(p.get("u", ""))
        if user is None or "t" not in p or "s" not in p:
            raise Fault(ERR_WRONG_CREDENTIALS, "Wrong username or password")
        if p["t"] != hashlib.md5((user.password + p["s"]).encode()).hexdigest():
            raise Fault(ERR_WRONG_CREDENTIALS, "Wrong username or password")
        return user

    # ---------------------------------------------------------------- endpoints

    def _ep_ping(self, user: FakeUser, params: list[tuple[str, str]]) -> dict[str, Any]:
        return {}

    def _song_json(self, s: FakeSong) -> dict[str, Any]:
        return {
            "id": s.id,
            "title": s.title,
            "artist": s.artist,
            "album": s.album,
            "duration": s.duration,
            "isDir": False,
            "type": "music",
        }

    def _ep_search3(self, user: FakeUser, params: list[tuple[str, str]]) -> dict[str, Any]:
        p = dict(params)
        words = p.get("query", "").lower().split()
        count = int(p.get("songCount", "20"))
        offset = int(p.get("songOffset", "0"))
        # Like Navidrome's full-text search: every query word must prefix a word of title / artist / album.
        def _tokens(song: FakeSong) -> list[str]:
            return re.findall(r"\w+", f"{song.title} {song.artist} {song.album}".lower())

        hits = [s for s in self.state.songs if all(any(t.startswith(w) for t in _tokens(s)) for w in words)]
        page = hits[offset : offset + count]
        result: dict[str, Any] = {"song": [self._song_json(s) for s in page]} if page else {}
        return {"searchResult3": result}

    def _ep_getSong(self, user: FakeUser, params: list[tuple[str, str]]) -> dict[str, Any]:
        song = self.state.song(dict(params).get("id", ""))
        if song is None:
            raise Fault(ERR_NOT_FOUND, "Song not found")
        return {"song": self._song_json(song)}

    def _visible_playlists(self, user: FakeUser) -> list[FakePlaylist]:
        return list(self.state.playlists.values())  # like Navidrome: own + other users' public playlists

    def _playlist_json(self, p: FakePlaylist, with_entries: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": p.id,
            "name": p.name,
            "owner": p.owner,
            "comment": p.comment,
            "songCount": len(p.entries),
            "public": False,
        }
        if with_entries:
            out["entry"] = [self._song_json(self.state.song(i)) for i in p.entries if self.state.song(i)]  # type: ignore[arg-type]
        return out

    def _ep_getPlaylists(self, user: FakeUser, params: list[tuple[str, str]]) -> dict[str, Any]:
        items = [self._playlist_json(p, False) for p in self._visible_playlists(user)]
        return {"playlists": {"playlist": items} if items else {}}

    def _own(self, user: FakeUser, playlist_id: str) -> FakePlaylist:
        p = self.state.playlists.get(playlist_id)
        if p is None:
            raise Fault(ERR_NOT_FOUND, "Playlist not found")
        if p.owner != user.username:
            raise Fault(ERR_NOT_AUTHORIZED, "Playlist is not owned by the user")
        return p

    def _ep_getPlaylist(self, user: FakeUser, params: list[tuple[str, str]]) -> dict[str, Any]:
        p = self.state.playlists.get(dict(params).get("id", ""))
        if p is None:
            raise Fault(ERR_NOT_FOUND, "Playlist not found")
        return {"playlist": self._playlist_json(p, True)}

    def _check_songs(self, ids: list[str]) -> None:
        for song_id in ids:
            if self.state.song(song_id) is None:
                raise Fault(ERR_NOT_FOUND, f"Song {song_id} not found")

    def _ep_createPlaylist(self, user: FakeUser, params: list[tuple[str, str]]) -> dict[str, Any]:
        p = dict(params)
        ids = [v for k, v in params if k == "songId"]
        self._check_songs(ids)
        if "playlistId" in p:  # documented: replaces the contents of an existing playlist
            target = self._own(user, p["playlistId"])
            target.entries = ids
            if "name" in p:
                target.name = p["name"]
        else:
            if "name" not in p:
                raise Fault(10, "Required parameter is missing: name")
            target = FakePlaylist(id=f"pl-{self.state.next_playlist_id}", name=p["name"], owner=user.username, entries=ids)
            self.state.next_playlist_id += 1
            self.state.playlists[target.id] = target
        return {"playlist": self._playlist_json(target, True)}

    def _ep_updatePlaylist(self, user: FakeUser, params: list[tuple[str, str]]) -> dict[str, Any]:
        p = dict(params)
        target = self._own(user, p.get("playlistId", ""))
        add = [v for k, v in params if k == "songIdToAdd"]
        sent = [int(v) for k, v in params if k == "songIndexToRemove"]
        remove = sorted(set(sent))
        self._check_songs(add)
        if self.state.sequential_removal:
            for idx in sent:  # in the order received, each against the list as it is now
                if not 0 <= idx < len(target.entries):
                    raise Fault(ERR_GENERIC, f"Index {idx} out of range")
                del target.entries[idx]
        else:
            for idx in remove:
                if not 0 <= idx < len(target.entries):
                    raise Fault(ERR_GENERIC, f"Index {idx} out of range")
            for idx in reversed(remove):
                del target.entries[idx]
        target.entries.extend(add)
        if "name" in p:
            target.name = p["name"]
        if "comment" in p:
            target.comment = p["comment"]
        return {}

    def _ep_deletePlaylist(self, user: FakeUser, params: list[tuple[str, str]]) -> dict[str, Any]:
        del self.state.playlists[self._own(user, dict(params).get("id", "")).id]
        return {}

    def _ep_startScan(self, user: FakeUser, params: list[tuple[str, str]]) -> dict[str, Any]:
        self.state.scans += 1
        return {"scanStatus": {"scanning": True, "count": 0}}

    def _ep_getUsers(self, user: FakeUser, params: list[tuple[str, str]]) -> dict[str, Any]:
        if not user.admin:
            raise Fault(ERR_NOT_AUTHORIZED, "User is not authorized for the given operation.")
        return {"users": {"user": [{"username": u.username, "adminRole": u.admin, "email": ""} for u in self.state.users.values()]}}


def default_state(songs: list[tuple[str, str, str]], password: str = "pw") -> SubsonicState:
    """State with an admin ``admin`` and a regular user ``kid`` (password ``pw``), one song per ``(title, artist, album)``."""
    st = SubsonicState(
        songs=[FakeSong(id=f"s{i}", title=t, artist=a, album=al) for i, (t, a, al) in enumerate(songs, start=1)],
        users={
            "admin": FakeUser("admin", admin=True, password=password),
            "kid": FakeUser("kid", admin=False, password=password),
        },
    )
    return st


def raw_json(status: int, body: dict[str, Any], headers: Optional[dict[str, str]] = None) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode(), headers={"content-type": "application/json", **(headers or {})})
