"""Regression: the MediaCoverService singleton must not reach the real network or leak work across tests.

A flaky full-suite failure in test_art_pipeline was caused by earlier tests (artist ingest, profile preview) queueing
real coverartarchive/Deezer downloads onto the shared pool, with a stale ``/data`` cache dir, which starved later waits.
"""

from pathlib import Path
from unittest.mock import patch

import requests

import trackseerr.mediacover as mc
from trackseerr.mediacover import mediacover_service

URL = "https://coverartarchive.org/release-group/isolation-check/front-500"


def test_cache_dirs_follow_the_per_test_base_dir(tmp_path):
    for d in (mediacover_service.base_dir, mediacover_service.artists_dir, mediacover_service.albums_dir):
        assert Path(d).is_relative_to(tmp_path), d


def test_real_http_is_blocked_by_default(tmp_path):
    with patch("socket.getaddrinfo") as resolver:
        assert mediacover_service.cache_image(tmp_path / "x.jpg", URL) is False
        assert mediacover_service.schedule_cache(tmp_path / "y.jpg", URL + "-2") is True
        assert mediacover_service.wait_idle(5)
    # never even resolved the image host (other threads may resolve unrelated names while the patch is active)
    assert not [c for c in resolver.call_args_list if "coverartarchive" in str(c)]
    assert not (tmp_path / "x.jpg").exists() and not (tmp_path / "y.jpg").exists()


def test_a_mocked_requests_get_still_wins(tmp_path):
    class _Resp:
        status_code = 200

        @staticmethod
        def iter_content(chunk_size=0):
            return [b"\xff\xd8\xff\xe0" + b"\x00" * 64]

    with patch("requests.get", return_value=_Resp()) as g:
        assert mediacover_service.cache_image(tmp_path / "z.jpg", URL) is True
    assert g.call_count == 1 and (tmp_path / "z.jpg").is_file()


def test_pool_state_is_reset_between_tests_part_1_dirty_the_service():
    mediacover_service._negative["https://dirty.example/a.jpg"] = 1e18
    mediacover_service._host_failures["dirty.example"] = 99
    mediacover_service._inflight["/stale"] = []


def test_pool_state_is_reset_between_tests_part_2_is_clean():
    assert not mediacover_service._negative and not mediacover_service._host_failures
    assert not mediacover_service._inflight
    assert isinstance(mc.requests.get, object) and requests  # shim exposes get
