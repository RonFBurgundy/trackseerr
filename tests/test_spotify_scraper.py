"""Tests for Keyless SpotifyWebScraper client."""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from trackseerr.clients.spotify_scraper import SpotifyWebScraper
from trackseerr.models import Playlist, Track


@pytest.fixture
def scraper():
    return SpotifyWebScraper(timeout=5)


def test_scraper_embed_success(scraper):
    embed_payload = {
        "props": {
            "pageProps": {
                "status": 200,
                "state": {
                    "data": {
                        "entity": {
                            "title": "Summer Hits 2026",
                            "subtitle": "The hottest summer tracks",
                            "coverArt": {
                                "sources": [
                                    {"url": "https://i.scdn.co/image/summer.jpg", "width": 300}
                                ]
                            },
                            "trackList": [
                                {
                                    "title": "Sunshine Reggae",
                                    "subtitle": "Laid Back",
                                    "duration": 210000,
                                    "uri": "spotify:track:12345",
                                },
                                {
                                    "title": "Cruel Summer",
                                    "subtitle": "Taylor Swift",
                                    "duration": 178000,
                                    "uri": "spotify:track:67890",
                                },
                            ],
                        }
                    }
                },
            }
        }
    }
    fake_html = f'<html><head><script id="__NEXT_DATA__" type="application/json">{json.dumps(embed_payload)}</script></head><body></body></html>'.encode("utf-8")

    mock_resp = MagicMock()
    mock_resp.read.return_value = fake_html
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        pl = scraper.get_playlist_by_id("37i9dQZF1DXsummer")
        assert pl is not None
        assert isinstance(pl, Playlist)
        assert pl.name == "Summer Hits 2026"
        assert pl.description == "The hottest summer tracks"
        assert pl.poster == "https://i.scdn.co/image/summer.jpg"

        tracks = scraper.get_playlist_tracks("37i9dQZF1DXsummer")
        assert len(tracks) == 2
        assert tracks[0].title == "Sunshine Reggae"
        assert tracks[0].artist == "Laid Back"
        assert tracks[1].title == "Cruel Summer"
        assert tracks[1].artist == "Taylor Swift"


def test_scraper_initial_state_fallback(scraper):
    initial_state_payload = {
        "entities": {
            "items": {
                "spotify:playlist:fallback_pl": {
                    "name": "Fallback Acoustic",
                    "description": "Acoustic chill tunes",
                    "images": [{"url": "https://i.scdn.co/image/acoustic.jpg"}],
                    "content": {
                        "items": [
                            {
                                "itemV2": {
                                    "data": {
                                        "name": "Fast Car",
                                        "artists": {
                                            "items": [{"profile": {"name": "Tracy Chapman"}}]
                                        },
                                        "albumOfTrack": {"name": "Tracy Chapman"},
                                        "duration": {"totalMilliseconds": 296000},
                                    }
                                }
                            }
                        ]
                    },
                }
            }
        }
    }
    encoded_b64 = base64.b64encode(json.dumps(initial_state_payload).encode("utf-8")).decode("utf-8")
    fake_embed_html = b"<html><head></head><body>No embed data here</body></html>"
    fake_page_html = f'<html><head><script id="initialState" type="text/plain">{encoded_b64}</script></head><body></body></html>'.encode("utf-8")

    def mock_urlopen_side_effect(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        resp = MagicMock()
        resp.__enter__.return_value = resp
        if "embed" in url:
            resp.read.return_value = fake_embed_html
        else:
            resp.read.return_value = fake_page_html
        return resp

    with patch("urllib.request.urlopen", side_effect=mock_urlopen_side_effect):
        pl = scraper.get_playlist_by_id("fallback_pl")
        assert pl is not None
        assert pl.name == "Fallback Acoustic"
        assert pl.poster == "https://i.scdn.co/image/acoustic.jpg"
        assert len(pl.tracks) == 1
        assert pl.tracks[0].title == "Fast Car"
        assert pl.tracks[0].artist == "Tracy Chapman"
        assert pl.tracks[0].album == "Tracy Chapman"


def test_scraper_404_not_found(scraper):
    embed_404 = {"props": {"pageProps": {"status": 404}}}
    fake_html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(embed_404)}</script>'.encode("utf-8")

    mock_resp = MagicMock()
    mock_resp.read.return_value = fake_html
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        pl = scraper.get_playlist_by_id("nonexistent_id")
        assert pl is None
        assert scraper.get_playlist_tracks("nonexistent_id") == []


def test_scraper_fetch_all_playlists(scraper):
    embed_payload = {
        "props": {
            "pageProps": {
                "status": 200,
                "state": {
                    "data": {
                        "entity": {
                            "title": "Batch Playlist",
                            "trackList": [{"title": "Track A", "subtitle": "Artist A"}],
                        }
                    }
                },
            }
        }
    }
    fake_html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(embed_payload)}</script>'.encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = fake_html
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        playlists = scraper.fetch_all_playlists(playlist_ids=["batch_1"], suffix=" - Scraped")
        assert len(playlists) == 1
        assert playlists[0].name == "Batch Playlist - Scraped"
        assert len(playlists[0].tracks) == 1
        assert playlists[0].tracks[0].title == "Track A"


def _entity(track_title):
    return {"title": "X", "trackList": [{"title": track_title, "subtitle": "B"}]}


def test_cache_hit_within_ttl(scraper):
    with patch.object(scraper, "_fetch_embed", return_value=_entity("A")) as fetch, patch(
        "trackseerr.clients.spotify_scraper.time.monotonic", side_effect=[1000.0, 1100.0]
    ):
        first = scraper._load_playlist("pl1")
        second = scraper._load_playlist("pl1")
    assert fetch.call_count == 1
    assert second is first


def test_cache_expires_after_ttl(scraper):
    with patch.object(
        scraper, "_fetch_embed", side_effect=[_entity("A"), _entity("New")]
    ) as fetch, patch(
        "trackseerr.clients.spotify_scraper.time.monotonic",
        side_effect=[1000.0, 1400.0, 1400.0],
    ):
        first = scraper._load_playlist("pl1")
        second = scraper._load_playlist("pl1")
    assert fetch.call_count == 2
    assert [t.title for t in first["tracks"]] == ["A"]
    assert [t.title for t in second["tracks"]] == ["New"]


def _embed_artists(scraper, subtitles):
    entity = {"title": "P", "subtitle": "", "trackList": [{"title": f"T{i}", "subtitle": sub} for i, sub in enumerate(subtitles)]}
    payload = {"props": {"pageProps": {"status": 200, "state": {"data": {"entity": entity}}}}}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'.encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = html
    resp.__enter__.return_value = resp
    with patch("urllib.request.urlopen", return_value=resp):
        return [t.artist for t in scraper.get_playlist_tracks("pl-artists")]


def test_embed_joined_artists_use_primary_only(scraper):
    artists = _embed_artists(scraper, ["The Offspring,\u00a0Redman", "Tyler, The Creator", "Taylor Swift"])
    assert artists == ["The Offspring", "Tyler, The Creator", "Taylor Swift"]


# --- pathfinder paging past the embed's 100-track cap -------------------------------------------

import logging  # noqa: E402
import urllib.error  # noqa: E402

from trackseerr.clients import spotify_scraper as ss  # noqa: E402


def _resp(body):
    r = MagicMock()
    r.read.return_value = body if isinstance(body, bytes) else body.encode("utf-8")
    r.__enter__.return_value = r
    return r


def _embed_html(n, token="TOK123"):
    entity = {"title": "Big", "trackList": [{"title": f"E{i}", "subtitle": "Art"} for i in range(n)]}
    payload = {"props": {"pageProps": {"status": 200, "state": {"data": {"entity": entity}}}}}
    tok = f'"accessToken":"{token}","accessTokenExpirationTimestampMs":1999999999999' if token else ""
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script><script>{{{tok}}}</script>'


def _pf_page(start, count, total):
    items = [
        {
            "itemV2": {
                "data": {
                    "name": f"P{start + i}",
                    "artists": {"items": [{"profile": {"name": "Page Artist"}}]},
                    "albumOfTrack": {"name": "Page Album"},
                    "trackDuration": {"totalMilliseconds": 200000},
                    "uri": f"spotify:track:{start + i}",
                }
            }
        }
        for i in range(count)
    ]
    return json.dumps({"data": {"playlistV2": {"content": {"totalCount": total, "items": items}}}})


def _router(embed_html, pathfinder, hash_pages=None):
    calls = {"pf": [], "other": []}

    def side(req, *a, **k):
        url = req.full_url
        if "pathfinder" in url:
            calls["pf"].append(json.loads(req.data))
            out = pathfinder(len(calls["pf"]))
            if isinstance(out, Exception):
                raise out
            return _resp(out)
        calls["other"].append(url)
        if "/embed/" in url:
            return _resp(embed_html)
        if hash_pages and url in hash_pages:
            return _resp(hash_pages[url])
        raise urllib.error.URLError("no network")

    return side, calls


def _http(code):
    return urllib.error.HTTPError("u", code, "err", {}, None)


def test_pages_past_embed_cap(scraper):
    side, calls = _router(_embed_html(100), lambda n: _pf_page(100, 50, 150))
    with patch("urllib.request.urlopen", side_effect=side):
        tracks = scraper.get_playlist_tracks("big")
    assert len(tracks) == 150
    assert calls["pf"][0]["variables"] == {"uri": "spotify:playlist:big", "offset": 100, "limit": 100}
    assert tracks[0].title == "E0" and tracks[0].album == ""
    assert (tracks[100].title, tracks[100].artist, tracks[100].album) == ("P100", "Page Artist", "Page Album")
    assert tracks[100].duration_seconds == 200.0


def test_short_embed_skips_pathfinder(scraper):
    side, calls = _router(_embed_html(40), lambda n: _pf_page(100, 50, 150))
    with patch("urllib.request.urlopen", side_effect=side):
        assert len(scraper.get_playlist_tracks("small")) == 40
    assert calls["pf"] == []


def test_no_token_skips_pathfinder(scraper):
    side, calls = _router(_embed_html(100, token=None), lambda n: _pf_page(100, 50, 150))
    with patch("urllib.request.urlopen", side_effect=side):
        assert len(scraper.get_playlist_tracks("notok")) == 100
    assert calls["pf"] == []


def test_pathfinder_429_keeps_embed_tracks_and_warns(scraper, caplog):
    side, _ = _router(_embed_html(100), lambda n: _http(429))
    with patch("urllib.request.urlopen", side_effect=side), caplog.at_level(logging.WARNING):
        assert len(scraper.get_playlist_tracks("rl")) == 100
    assert "first 100 tracks only" in caplog.text


def test_pathfinder_412_rediscovers_hash_once(scraper):
    new_hash = "a" * 64
    pages = {
        "https://open.spotify.com/": '<script src="https://open.spotifycdn.com/cdn/build/web-player/web-player.abc123.js">',
        "https://open.spotifycdn.com/cdn/build/web-player/web-player.abc123.js": f'x"fetchPlaylistContents","query","{new_hash}"y',
    }
    side, calls = _router(_embed_html(100), lambda n: _http(412) if n == 1 else _pf_page(100, 50, 150), pages)
    with patch("urllib.request.urlopen", side_effect=side):
        tracks = scraper.get_playlist_tracks("stale")
    assert len(tracks) == 150
    assert calls["pf"][-1]["extensions"]["persistedQuery"]["sha256Hash"] == new_hash
    assert calls["other"].count("https://open.spotify.com/") == 2  # initial discovery + one rediscovery


def test_412_twice_gives_up(scraper, caplog):
    side, calls = _router(_embed_html(100), lambda n: _http(412))
    with patch("urllib.request.urlopen", side_effect=side), caplog.at_level(logging.WARNING):
        assert len(scraper.get_playlist_tracks("stale2")) == 100
    assert len(calls["pf"]) == 2
    assert "first 100 tracks only" in caplog.text


def test_hash_discovery_extracts_from_bundle(scraper):
    h = "b" * 64
    pages = {
        "https://open.spotify.com/": 'a https://open.spotifycdn.com/cdn/build/web-player/web-player.0f9e.js b',
        "https://open.spotifycdn.com/cdn/build/web-player/web-player.0f9e.js": f'"fetchPlaylist","query","{h}"',
    }
    side, _ = _router("", lambda n: "", pages)
    with patch("urllib.request.urlopen", side_effect=side):
        assert scraper._pathfinder_hash() == h


def test_hash_discovery_failure_uses_default_and_caches(scraper):
    side, calls = _router("", lambda n: "")
    with patch("urllib.request.urlopen", side_effect=side):
        assert scraper._pathfinder_hash() == ss.DEFAULT_PATHFINDER_HASH
        assert scraper._pathfinder_hash() == ss.DEFAULT_PATHFINDER_HASH
    assert len(calls["other"]) == 1


def test_max_tracks_stops_paging(scraper):
    side, calls = _router("", lambda n: _pf_page(100 * n, 100, 5000))
    with patch("urllib.request.urlopen", side_effect=side):
        out = scraper._fetch_remaining("pl", "tok", 100, max_tracks=300)
    assert len(out) == 200
    assert len(calls["pf"]) == 2


def test_empty_page_stops_and_blank_names_skipped(scraper):
    pages = [_pf_page(100, 2, 500), _pf_page(0, 0, 500)]
    side, calls = _router("", lambda n: pages[n - 1])
    with patch("urllib.request.urlopen", side_effect=side):
        out = scraper._fetch_remaining("pl", "tok", 100)
    assert len(out) == 2 and len(calls["pf"]) == 2
    assert scraper._parse_pathfinder_items([{"itemV2": {"data": {"name": ""}}}, {}]) == []
