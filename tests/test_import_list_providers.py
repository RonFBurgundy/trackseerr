"""Import list providers with mocked HTTP: pagination, MBID handling, retries and error handling."""

from typing import Any, Optional
from unittest.mock import patch

import pytest

from plex_playlist_sync.clients.import_lists import (
    PROVIDERS,
    SECRET_KEYS,
    ImportListError,
    fetch_items,
    lastfm,
    listenbrainz,
    musicbrainz_collection,
    provider_metadata,
)

MBID_A = "11111111-1111-1111-1111-111111111111"
MBID_B = "22222222-2222-2222-2222-222222222222"
MBID_C = "33333333-3333-3333-3333-333333333333"
PL_1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PL_2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class FakeResp:
    def __init__(self, payload: Any = None, status: int = 200, headers: Optional[dict[str, str]] = None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture(autouse=True)
def _no_sleep():
    # base and musicbrainz_collection share the one ``time`` module, so a single patch covers both.
    with patch("plex_playlist_sync.clients.import_lists.base.time.sleep") as sleep:
        yield sleep


def _get(responses: list[FakeResp]):
    return patch("plex_playlist_sync.clients.import_lists.base.requests.get", side_effect=responses)


# ------------------------------------------------------------------ Last.fm


def _lf_tracks(names: list[tuple[str, str, str]], page: int, pages: int) -> FakeResp:
    return FakeResp(
        {
            "lovedtracks": {
                "track": [{"name": n, "mbid": m, "artist": {"name": a, "mbid": ""}} for n, a, m in names],
                "@attr": {"page": str(page), "totalPages": str(pages)},
            }
        }
    )


def test_lastfm_paginates_until_limit_and_keeps_mbids():
    cfg = {"username": "ron", "api_key": "k", "source": "loved_tracks", "limit": 5}
    pages = [
        _lf_tracks([("T1", "A1", MBID_A), ("T2", "A2", ""), ("T3", "A3", "")], 1, 2),
        _lf_tracks([("T4", "A4", ""), ("T5", "A5", ""), ("T6", "A6", "")], 2, 2),
    ]
    with _get(pages) as get:
        items = lastfm.fetch(cfg)
    assert [i.track_title for i in items] == ["T1", "T2", "T3", "T4", "T5"]
    assert get.call_count == 2
    assert [c.kwargs["params"]["page"] for c in get.call_args_list] == [1, 2]
    assert items[0].mbid == MBID_A
    assert items[1].mbid is None  # MBID-less: resolved later by name
    assert items[1].external_key == "a2|t2"
    assert all(i.kind == "track" for i in items)


def test_lastfm_stops_at_last_page():
    cfg = {"username": "ron", "api_key": "k", "source": "loved_tracks", "limit": 100}
    with _get([_lf_tracks([("T1", "A1", "")], 1, 1)]) as get:
        items = lastfm.fetch(cfg)
    assert len(items) == 1 and get.call_count == 1


def test_lastfm_top_artists_and_albums():
    artists = FakeResp(
        {"topartists": {"artist": [{"name": "Band", "mbid": MBID_B}, {"name": "No Id", "mbid": ""}], "@attr": {"totalPages": "1"}}}
    )
    with _get([artists]) as get:
        items = lastfm.fetch({"username": "u", "api_key": "k", "source": "top_artists", "period": "7day"})
    assert get.call_args.kwargs["params"]["period"] == "7day"
    assert get.call_args.kwargs["params"]["method"] == "user.gettopartists"
    assert (items[0].kind, items[0].mbid, items[0].external_key) == ("artist", MBID_B, MBID_B)
    assert (items[1].mbid, items[1].external_key) == (None, "no id")

    albums = FakeResp(
        {
            "topalbums": {
                "album": [{"name": "LP", "mbid": MBID_C, "artist": {"name": "Band", "mbid": MBID_B}}],
                "@attr": {"totalPages": "1"},
            }
        }
    )
    with _get([albums]):
        items = lastfm.fetch({"username": "u", "api_key": "k", "source": "top_albums"})
    # Last.fm album ids are release ids, so they are not kept as the release-group mbid.
    assert (items[0].kind, items[0].mbid, items[0].artist_mbid, items[0].album_title) == ("album", None, MBID_B, "LP")


def test_lastfm_single_item_dict_is_wrapped():
    resp = FakeResp({"toptracks": {"track": {"name": "Solo", "mbid": "", "artist": {"name": "X", "mbid": ""}}, "@attr": {"totalPages": "1"}}})
    with _get([resp]):
        items = lastfm.fetch({"username": "u", "api_key": "k", "source": "top_tracks"})
    assert [i.track_title for i in items] == ["Solo"]


def test_lastfm_api_key_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("LASTFM_API_KEY", "env-key")
    with _get([_lf_tracks([("T", "A", "")], 1, 1)]) as get:
        lastfm.fetch({"username": "u", "api_key": "", "source": "loved_tracks"})
    assert get.call_args.kwargs["params"]["api_key"] == "env-key"


def test_lastfm_requires_key_username_and_valid_source(monkeypatch):
    monkeypatch.delenv("LASTFM_API_KEY", raising=False)
    with pytest.raises(ImportListError, match="API key"):
        lastfm.fetch({"username": "u", "source": "loved_tracks"})
    with pytest.raises(ImportListError, match="username"):
        lastfm.fetch({"api_key": "k", "source": "loved_tracks"})
    with pytest.raises(ImportListError, match="source"):
        lastfm.fetch({"username": "u", "api_key": "k", "source": "bogus"})
    with pytest.raises(ImportListError, match="limit"):
        lastfm.fetch({"username": "u", "api_key": "k", "source": "loved_tracks", "limit": "many"})


def test_lastfm_error_payload_does_not_leak_key():
    with _get([FakeResp({"error": 6, "message": "User not found"})]):
        with pytest.raises(ImportListError) as exc:
            lastfm.fetch({"username": "u", "api_key": "SECRETKEY", "source": "loved_tracks"})
    assert "User not found" in str(exc.value) and "SECRETKEY" not in str(exc.value)


def test_retry_on_429_then_success_and_exhaustion(_no_sleep):
    ok = _lf_tracks([("T", "A", "")], 1, 1)
    with _get([FakeResp(status=429, headers={"Retry-After": "2"}), FakeResp(status=503), ok]) as get:
        items = lastfm.fetch({"username": "u", "api_key": "k", "source": "loved_tracks"})
    assert len(items) == 1 and get.call_count == 3
    assert _no_sleep.call_args_list[0].args[0] == 2.0

    with _get([FakeResp(status=500)] * 4) as get:
        with pytest.raises(ImportListError, match="HTTP 500"):
            lastfm.fetch({"username": "u", "api_key": "SECRETKEY", "source": "loved_tracks"})
    assert get.call_count == 4


def test_timeouts_are_passed_and_network_errors_retried():
    import requests

    with patch(
        "plex_playlist_sync.clients.import_lists.base.requests.get",
        side_effect=[requests.ConnectionError("boom"), _lf_tracks([("T", "A", "")], 1, 1)],
    ) as get:
        lastfm.fetch({"username": "u", "api_key": "k", "source": "loved_tracks"})
    assert all(c.kwargs["timeout"] > 0 for c in get.call_args_list)


# ------------------------------------------------------------------ ListenBrainz


def _jspf(tracks: list[dict[str, Any]]) -> FakeResp:
    return FakeResp({"playlist": {"title": "Weekly Jams", "track": tracks}})


def _jspf_track(rec: str, title: str, artist: str, artist_mbid: Optional[str] = None, album: str = "") -> dict[str, Any]:
    t: dict[str, Any] = {"identifier": [f"https://musicbrainz.org/recording/{rec}"], "title": title, "creator": artist, "album": album}
    if artist_mbid:
        t["extension"] = {
            "https://musicbrainz.org/doc/jspf#track": {"additional_metadata": {"artists": [{"artist_mbid": artist_mbid}]}}
        }
    return t


def test_listenbrainz_playlist_uses_mbids_and_sends_token():
    with _get([_jspf([_jspf_track(MBID_A, "Song", "Band", MBID_B, "LP")])]) as get:
        items = listenbrainz.fetch({"source": "playlist", "playlist_mbid": f"https://listenbrainz.org/playlist/{PL_1}/", "token": "tok"})
    assert get.call_args.args[0].endswith(f"/playlist/{PL_1}")
    assert get.call_args.kwargs["headers"]["Authorization"] == "Token tok"
    it = items[0]
    assert (it.kind, it.mbid, it.artist_mbid, it.album_title, it.external_key) == ("track", MBID_A, MBID_B, "LP", MBID_A)


def test_listenbrainz_created_for_pages_through_playlists_and_fetches_each():
    def listing(mbids: list[str], total: int) -> FakeResp:
        return FakeResp(
            {
                "playlists": [{"playlist": {"identifier": f"https://listenbrainz.org/playlist/{m}"}} for m in mbids],
                "playlist_count": total,
            }
        )

    responses = [
        listing([PL_1] * 25, 26),  # first page: 25 entries (same id repeated is de-duplicated)
        listing([PL_2], 26),
        _jspf([_jspf_track(MBID_A, "One", "A1"), _jspf_track(MBID_B, "Two", "A2")]),
        _jspf([_jspf_track(MBID_B, "Two", "A2"), _jspf_track(MBID_C, "Three", "A3")]),
    ]
    with _get(responses) as get:
        items = listenbrainz.fetch({"source": "created_for", "username": "ron"})
    urls = [c.args[0] for c in get.call_args_list]
    assert urls[0].endswith("/user/ron/playlists/createdfor") and urls[1].endswith("/user/ron/playlists/createdfor")
    assert get.call_args_list[1].kwargs["params"]["offset"] == 25
    assert urls[2].endswith(PL_1) and urls[3].endswith(PL_2)
    assert [i.mbid for i in items] == [MBID_A, MBID_B, MBID_C]  # overlap across playlists collapsed


def test_listenbrainz_top_artists_paginates_and_handles_204():
    page1 = FakeResp({"payload": {"artists": [{"artist_name": f"A{i}", "artist_mbids": [MBID_A]} for i in range(100)]}})
    page2 = FakeResp({"payload": {"artists": [{"artist_name": "Last", "artist_mbids": []}]}})
    with _get([page1, page2]) as get:
        items = listenbrainz.fetch({"source": "top_artists", "username": "u", "range": "month", "limit": 150})
    assert get.call_args_list[1].kwargs["params"]["offset"] == 100
    assert get.call_args_list[0].kwargs["params"]["range"] == "month"
    assert items[0].mbid == MBID_A and items[-1].mbid is None and items[-1].external_key == "last"
    # identical mbid on 100 rows collapses to one item + the last one
    assert len(items) == 2

    with _get([FakeResp(status=204)]):
        assert listenbrainz.fetch({"source": "top_artists", "username": "u"}) == []


def test_listenbrainz_top_release_groups():
    resp = FakeResp(
        {
            "payload": {
                "release_groups": [
                    {"release_group_name": "LP", "release_group_mbid": MBID_A, "artist_name": "Band", "artist_mbids": [MBID_B]}
                ]
            }
        }
    )
    with _get([resp]):
        items = listenbrainz.fetch({"source": "top_release_groups", "username": "u"})
    assert (items[0].kind, items[0].mbid, items[0].artist_mbid, items[0].album_title) == ("album", MBID_A, MBID_B, "LP")


def test_listenbrainz_validation():
    with pytest.raises(ImportListError, match="source"):
        listenbrainz.fetch({"source": "nope"})
    with pytest.raises(ImportListError, match="playlist"):
        listenbrainz.fetch({"source": "playlist", "playlist_mbid": "not-an-id"})
    with pytest.raises(ImportListError, match="username"):
        listenbrainz.fetch({"source": "top_artists"})
    with pytest.raises(ImportListError, match="range"):
        listenbrainz.fetch({"source": "top_artists", "username": "u", "range": "decade"})


# ------------------------------------------------------------------ MusicBrainz collections


def test_mb_collection_release_group_type():
    resp = FakeResp(
        {
            "release-group-count": 1,
            "release-groups": [
                {"id": MBID_A, "title": "LP", "artist-credit": [{"name": "Band", "artist": {"id": MBID_B, "name": "Band"}}]}
            ],
        }
    )
    with _get([resp]) as get:
        items = musicbrainz_collection.fetch({"collection_mbid": PL_1}, mb_base_url="https://mirror.test")
    assert get.call_args.args[0] == "https://mirror.test/ws/2/release-group"
    assert get.call_args.kwargs["params"]["collection"] == PL_1
    assert (items[0].kind, items[0].mbid, items[0].artist_mbid, items[0].artist_name) == ("album", MBID_A, MBID_B, "Band")


def test_mb_collection_artist_type_after_wrong_type_400_and_pagination():
    page1 = FakeResp({"artist-count": 101, "artists": [{"id": f"{i:08d}-0000-0000-0000-000000000000", "name": f"A{i}"} for i in range(100)]})
    page2 = FakeResp({"artist-count": 101, "artists": [{"id": MBID_A, "name": "Last"}]})
    with _get([FakeResp(status=400), page1, page2]) as get:
        items = musicbrainz_collection.fetch({"collection_mbid": PL_1})
    assert [c.args[0].rsplit("/", 1)[1] for c in get.call_args_list] == ["release-group", "artist", "artist"]
    assert get.call_args_list[2].kwargs["params"]["offset"] == 100
    assert len(items) == 101 and all(i.kind == "artist" for i in items)
    assert items[-1].mbid == MBID_A


def test_mb_collection_release_type_maps_to_release_group():
    releases = FakeResp(
        {
            "release-count": 2,
            "releases": [
                {
                    "id": "r1",
                    "title": "LP (deluxe)",
                    "artist-credit": [{"name": "Band", "artist": {"id": MBID_B, "name": "Band"}}],
                    "release-group": {"id": MBID_A, "title": "LP"},
                },
                {  # a second release of the same release group collapses
                    "id": "r2",
                    "title": "LP",
                    "artist-credit": [{"name": "Band", "artist": {"id": MBID_B, "name": "Band"}}],
                    "release-group": {"id": MBID_A, "title": "LP"},
                },
            ],
        }
    )
    with _get([FakeResp(status=400), FakeResp(status=400), releases]):
        items = musicbrainz_collection.fetch({"collection_mbid": PL_1})
    assert len(items) == 1
    assert (items[0].kind, items[0].mbid, items[0].album_title, items[0].artist_mbid) == ("album", MBID_A, "LP", MBID_B)


def test_mb_collection_validation_and_server_error():
    with pytest.raises(ImportListError, match="collection"):
        musicbrainz_collection.fetch({"collection_mbid": "nope"})
    with _get([FakeResp(status=500)] * 4):
        with pytest.raises(ImportListError, match="HTTP 500"):
            musicbrainz_collection.fetch({"collection_mbid": PL_1})


# ------------------------------------------------------------------ registry


def test_registry_metadata_shape_and_secret_keys():
    meta = {m["provider"]: m for m in provider_metadata()}
    assert set(meta) == {"lastfm", "listenbrainz", "musicbrainz_collection"} == set(PROVIDERS)
    for m in meta.values():
        assert m["label"] and m["sources"]
        for f in m["fields"]:
            assert f["type"] in ("text", "secret", "select", "number")
            assert isinstance(f["required"], bool)
            assert (f["type"] != "select") or f["options"]
    assert SECRET_KEYS == {"lastfm": ("api_key",), "listenbrainz": ("token",), "musicbrainz_collection": ()}
    assert "fetch" not in meta["lastfm"]


def test_fetch_items_unknown_provider():
    with pytest.raises(ImportListError, match="Unknown"):
        fetch_items("spotify", {})


# ------------------------------------------------------------------ review fixes


def test_listenbrainz_username_is_percent_encoded_in_urls():
    with _get([FakeResp(None, status=204)]) as get:
        listenbrainz.fetch({"source": "top_artists", "username": "Some User+1"})
    assert "/stats/user/Some%20User%2B1/artists" in get.call_args.args[0]
    with _get([FakeResp({"playlists": [], "playlist_count": 0})]) as get:
        listenbrainz.fetch({"source": "created_for", "username": "Some User+1"})
    assert "/user/Some%20User%2B1/playlists/createdfor" in get.call_args.args[0]


@pytest.mark.parametrize("name", ["../admin", "a/b", "..", "x?y", "x#y", "a\\b", "100%", "a\nb", "x" * 65])
@pytest.mark.parametrize("source", ["top_artists", "created_for"])
def test_listenbrainz_rejects_path_traversal_usernames(name, source):
    with _get([]) as get:
        with pytest.raises(ImportListError, match="invalid characters"):
            listenbrainz.fetch({"source": source, "username": name})
    get.assert_not_called()


def test_mb_collection_entity_probes_are_rate_limited_on_musicbrainz_org(_no_sleep):
    musicbrainz_collection._last_request_at = 0.0
    with patch("plex_playlist_sync.clients.import_lists.musicbrainz_collection.time.monotonic", return_value=1000.0):
        musicbrainz_collection._last_request_at = 1000.0  # a request was just made
        with _get([FakeResp(status=400), FakeResp(status=400), FakeResp({"release-count": 0, "releases": []})]):
            musicbrainz_collection.fetch({"collection_mbid": PL_1})
    waits = [c.args[0] for c in _no_sleep.call_args_list]
    assert waits and all(w == pytest.approx(1.0) for w in waits)
    assert len(waits) == 3  # one wait before each of the three probes


def test_mb_collection_mirror_is_not_throttled(_no_sleep):
    musicbrainz_collection._last_request_at = 0.0
    with _get([FakeResp({"release-group-count": 0, "release-groups": []}), FakeResp(status=400), FakeResp(status=400)]):
        musicbrainz_collection.fetch({"collection_mbid": PL_1}, mb_base_url="https://mirror.test")
    _no_sleep.assert_not_called()
