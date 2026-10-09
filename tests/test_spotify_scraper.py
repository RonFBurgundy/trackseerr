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
