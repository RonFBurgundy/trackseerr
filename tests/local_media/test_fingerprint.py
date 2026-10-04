"""AcoustID / Chromaprint fingerprinting on real MP3s.

Generation needs the ``fpcalc`` binary and the ``pyacoustid`` package; neither ships in the test image, so the
generation test skips there (and runs on a machine that has both). Lookups additionally need ACOUSTID_API_KEY
and network. The orchestration around them is tested offline with a stand-in ``acoustid`` module.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import types
from pathlib import Path

import mutagen
import pytest

from plex_playlist_sync.library import fingerprint_audio_file

from .conftest import configure_roots

pytestmark = pytest.mark.local_media

REPO = Path(__file__).resolve().parents[2]
HAVE_FPCALC = shutil.which("fpcalc") is not None
HAVE_ACOUSTID = importlib.util.find_spec("acoustid") is not None


@pytest.fixture
def mp3(tmp_path, copy_discovery) -> Path:
    return copy_discovery(["02 Aerodynamic.mp3"], tmp_path / "fp")[0]


def test_no_api_key_means_no_lookup(mp3):
    assert fingerprint_audio_file(mp3, api_key=None) is None


def test_missing_file_returns_none(tmp_path):
    assert fingerprint_audio_file(tmp_path / "ghost.mp3", api_key="k") is None


def test_best_match_is_mapped_from_acoustid_results(mp3, monkeypatch):
    fake = types.ModuleType("acoustid")
    calls = []

    def match(key, path):
        calls.append((key, path))
        yield (0.97, "rec-123", "Aerodynamic", "Daft Punk")
        yield (0.40, "rec-999", "Other", "Other")

    fake.match = match
    monkeypatch.setitem(sys.modules, "acoustid", fake)
    out = fingerprint_audio_file(mp3, api_key="k")
    assert out == {"score": 0.97, "recording_id": "rec-123", "title": "Aerodynamic", "artist": "Daft Punk"}
    assert calls == [("k", str(mp3.resolve()))]


def test_acoustid_failure_is_swallowed(mp3, monkeypatch):
    fake = types.ModuleType("acoustid")

    def match(key, path):
        raise RuntimeError("fpcalc exploded")

    fake.match = match
    monkeypatch.setitem(sys.modules, "acoustid", fake)
    assert fingerprint_audio_file(mp3, api_key="k") is None


def test_fingerprint_route_degrades_gracefully_without_key(api, tmp_path, mp3):
    configure_roots(api.db, mp3.parent)
    resp = api.post("/api/library/manual-import/fingerprint", {"file_path": str(mp3)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False and "unavailable" in body["message"].lower()


def test_fingerprint_route_rejects_missing_file_and_outside_paths(api, mp3):
    assert api.post("/api/library/manual-import/fingerprint", {"file_path": str(mp3.parent / "no.mp3")}).status_code == 400
    assert api.post("/api/library/manual-import/fingerprint", {"file_path": "/etc/passwd"}).status_code == 403


@pytest.mark.skipif(not (HAVE_FPCALC and HAVE_ACOUSTID), reason="needs fpcalc (chromaprint) and pyacoustid")
def test_fingerprint_generation_on_real_mp3(mp3):
    import acoustid

    duration, fp = acoustid.fingerprint_file(str(mp3))
    assert fp and len(fp) > 100
    assert abs(duration - mutagen.File(str(mp3)).info.length) < 2


@pytest.mark.network
@pytest.mark.skipif(
    not (HAVE_FPCALC and HAVE_ACOUSTID and os.environ.get("ACOUSTID_API_KEY")),
    reason="needs fpcalc, pyacoustid and ACOUSTID_API_KEY",
)
def test_acoustid_lookup_identifies_aerodynamic(mp3):
    out = fingerprint_audio_file(mp3, api_key=os.environ["ACOUSTID_API_KEY"])
    assert out is not None and out["title"] and "aerodynamic" in out["title"].lower()


@pytest.mark.xfail(
    strict=True,
    reason="BUG: fingerprinting can never work in the shipped image - neither pyacoustid nor chromaprint/fpcalc is "
    "in requirements.txt or the Dockerfile, and fingerprint_audio_file (library.py:643) silently returns None when "
    "the import fails. Fix: add `pyacoustid` to requirements.txt and `apt-get install libchromaprint-tools` to the Dockerfile",
)
def test_shipped_image_declares_acoustid_and_chromaprint():
    reqs = (REPO / "requirements.txt").read_text().lower()
    docker = (REPO / "Dockerfile").read_text().lower()
    assert "pyacoustid" in reqs or "acoustid" in reqs
    assert "chromaprint" in docker
