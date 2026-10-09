"""Opt-in harness for tests that run against a real, local music library (copyrighted: never committed).

Enable with ``RUN_LOCAL_MEDIA=1`` and/or ``TRACKSEERR_LOCAL_MEDIA=<dir>``. Without either, pytest does not
collect this directory, so the normal suite is unaffected. When enabled but the directory is missing, every
test skips. Source media is only ever READ; fixtures copy the needed files into ``tmp_path`` first.
See docs/LOCAL_MEDIA_TESTS.md.
"""

from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

ENV_MEDIA = "TRACKSEERR_LOCAL_MEDIA"
ENV_RUN = "RUN_LOCAL_MEDIA"

_ENABLED = os.environ.get(ENV_RUN) == "1" or bool(os.environ.get(ENV_MEDIA))
collect_ignore_glob = [] if _ENABLED else ["test_*.py"]

# Files copied for the shared "mini" Discovery library. Chosen to hit every real-world mess at once:
#   OMT x3          iTunes duplicates, combined artist tag "Daft Punk/Romanthony"
#   Aerodynamic     clean control, TDRC "2001-01-01"
#   Night Vision /  same track 6, two title spellings (TDRC "2001" on the latter)
#   Nightvision
#   Face to Face    combined artist tag "Daft Punk/Todd Edwards"
#   Around The World  artist tag "Daft punk" (case), NO track-number tag, belongs to a different album really
MINI_SET = [
    "01 One More Time.mp3",
    "01 One More Time 2.mp3",
    "01 One More Time 4.mp3",
    "02 Aerodynamic.mp3",
    "06 Night Vision.mp3",
    "06 Nightvision.mp3",
    "13 Face to Face.mp3",
    "Around The World.mp3",
]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "local_media: needs a real local music library (TRACKSEERR_LOCAL_MEDIA)")
    config.addinivalue_line("markers", "network: needs outbound network access (MusicBrainz etc.)")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "local_media" in str(item.fspath):
            item.add_marker(pytest.mark.local_media)


@pytest.fixture(scope="session")
def media_root() -> Path:
    raw = os.environ.get(ENV_MEDIA)
    if not raw or not Path(raw).is_dir():
        pytest.skip(f"{ENV_MEDIA} does not point at an existing directory")
    return Path(raw)


@pytest.fixture(scope="session")
def discovery_src(media_root: Path) -> Path:
    src = media_root / "Daft Punk" / "Discovery"
    if not src.is_dir():
        pytest.skip(f"{src} not found")
    return src


def copy_media(src_dir: Path, names: list[str], dst_dir: Path) -> list[Path]:
    """Copies ``names`` from the read-only source into ``dst_dir`` (content only: NTFS metadata is irrelevant)."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for name in names:
        src = src_dir / name
        if not src.is_file():
            pytest.skip(f"fixture file missing from local library: {src}")
        dst = dst_dir / name
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o644)
        out.append(dst)
    return out


@pytest.fixture
def copy_discovery(discovery_src: Path):
    """Factory: ``copy_discovery(names, dst_dir)`` -> list of copied paths."""
    return lambda names, dst: copy_media(discovery_src, names, dst)


@pytest.fixture(scope="session")
def pristine_root(discovery_src: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Shared, READ-ONLY-BY-CONVENTION music root holding the mini Discovery set. Mutating tests must copy it."""
    root = tmp_path_factory.mktemp("pristine") / "music"
    copy_media(discovery_src, MINI_SET, root / "Daft Punk" / "Discovery")
    return root


@pytest.fixture
def library_copy(pristine_root: Path, tmp_path: Path) -> Path:
    """A private mutable copy of the mini library under tmp_path."""
    dst = tmp_path / "music"
    shutil.copytree(pristine_root, dst)
    return dst


@contextmanager
def no_background_hydration() -> Iterator[None]:
    """The scanner launches a daemon thread that hydrates new artists from MusicBrainz/Deezer. Never in tests."""
    from trackseerr.artist_refresh_worker import artist_refresh_worker

    with patch.object(artist_refresh_worker, "refresh_once", lambda *a, **k: {}):
        yield


def run_scan(db, root: Path, **kwargs) -> dict:
    from trackseerr.library_scanner import LibraryScanner

    with no_background_hydration():
        return LibraryScanner().scan(db, root_folder=str(root), **kwargs)


def snapshot(db) -> dict:
    """Flat view of the catalog: artists, albums, tracks (with their files)."""
    artists = db.list_library_artists(limit=1000)
    albums = db.list_library_albums(limit=1000)
    tracks = db.list_library_tracks(limit=1000)
    files = db.list_library_files(limit=1000)
    by_track: dict[str, list[dict]] = {}
    for f in files:
        by_track.setdefault(f["track_id"], []).append(f)
    return {"artists": artists, "albums": albums, "tracks": tracks, "files": files, "files_by_track": by_track}


@pytest.fixture
def db(tmp_path: Path):
    from trackseerr.storage import Database

    database = Database(str(tmp_path / "local_media.db"))
    yield database
    database.close()


@pytest.fixture(scope="session")
def scanned_pristine(pristine_root: Path, tmp_path_factory: pytest.TempPathFactory):
    """Database + scan status from ONE scan of the shared mini library. Read-only for tests."""
    from trackseerr.storage import Database

    database = Database(str(tmp_path_factory.mktemp("pristine_db") / "scan.db"))
    status = run_scan(database, pristine_root)
    yield database, status
    database.close()


# --------------------------------------------------------------------------------------------------
# API client (same wiring as tests/test_library_api.py)
# --------------------------------------------------------------------------------------------------

class ApiEnv:
    def __init__(self, client, db, headers, plex, config):
        self.client = client
        self.db = db
        self.headers = headers
        self.plex = plex
        self.config = config

    def post(self, path: str, json=None):
        return self.client.post(path, json=json, headers=self.headers)

    def get(self, path: str):
        return self.client.get(path, headers=self.headers)


@pytest.fixture
def api(db, tmp_path: Path):
    """FastAPI TestClient bound to ``db`` with an admin session. Plex is a MagicMock."""
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from trackseerr.api.app import create_app
    from trackseerr.api.dependencies import get_config, get_db, get_plex_client
    from trackseerr.auth import create_session_token, get_or_create_secret_key
    from trackseerr.config import Config

    config = Config(plex_url="http://127.0.0.1:32400", plex_token="test-token", data_dir=str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir(exist_ok=True)
    admin = db.upsert_user("admin-1", "admin_user", "admin@example.com", is_admin=True)
    app = create_app(db=db, config=config)
    plex = MagicMock()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_plex_client] = lambda: plex
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=admin["id"], username=admin["username"], is_admin=True, secret_key=secret)
    db.create_session(token, admin["id"], {"auth": "test"})
    return ApiEnv(TestClient(app), db, {"Authorization": f"Bearer {token}"}, plex, config)


def configure_roots(db, root: Path, staging: Path | None = None, **extra) -> None:
    """Points media management at ``root`` (and optionally ``staging``); keeps all other settings at defaults."""
    settings = {"root_folder_path": str(root), **extra}
    if staging is not None:
        settings["staging_folder_path"] = str(staging)
    db.update_media_management_settings(settings)


def set_id3(path: Path, **frames) -> None:
    """Rewrites ID3 text frames on a COPIED mp3, e.g. ``set_id3(p, TPE1="Daft Punk", TDRC="2001")``."""
    from mutagen import id3

    tags = id3.ID3(str(path))
    for frame_id, value in frames.items():
        tags.delall(frame_id)
        if value is not None:
            tags.add(getattr(id3, frame_id)(encoding=3, text=[value]))
    tags.save(str(path))


def audio_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.suffix.lower() in {".mp3", ".m4a", ".flac"})
