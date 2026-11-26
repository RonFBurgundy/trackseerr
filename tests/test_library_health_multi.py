"""Multi-server library health: merged findings, per-server path mappings, error isolation, and API routes."""
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import library_health as lh
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_active_media_server, get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.media_servers import registry
from trackseerr.media_servers.base import MediaServerError, ServerCapabilities, ServerFileRef
from trackseerr.storage import Database

FULL_TAGS = {"artist": "A", "album": "B", "title": "C"}


class FakeServer:
    paths_relative = False

    def __init__(
        self,
        refs: list[ServerFileRef],
        *,
        file_paths: bool = True,
        roots: Optional[list[str]] = None,
        last_scan: Optional[datetime] = None,
        error: Optional[Exception] = None,
        kind: str = "plex",
    ):
        self.refs = refs
        self.roots = roots or []
        self.last_scan = last_scan
        self.error = error
        self.kind = kind
        self._caps = ServerCapabilities(file_paths=file_paths)

    @property
    def capabilities(self) -> ServerCapabilities:
        return self._caps

    def iter_library_files(self) -> Iterator[ServerFileRef]:
        if self.error:
            raise self.error
        yield from self.refs

    def last_scan_at(self) -> Optional[datetime]:
        return self.last_scan

    def library_roots(self) -> list[str]:
        return self.roots


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "health.db"))
    yield d
    d.close()


@pytest.fixture
def music(tmp_path: Path) -> Path:
    m = tmp_path / "music"
    m.mkdir()
    return m


@pytest.fixture(autouse=True)
def tags_ok(monkeypatch):
    monkeypatch.setattr(lh, "check_magic", lambda p: None)
    monkeypatch.setattr(lh, "inspect_audio_file", lambda p: dict(FULL_TAGS))


@pytest.fixture(autouse=True)
def reset_registry():
    registry._reset_for_tests()
    yield
    registry._reset_for_tests()


def touch(path: Path, age_days: float = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    t = time.time() - age_days * 86400
    os.utime(path, (t, t))
    return path


def ref(root: str, music: Path, path: Path) -> ServerFileRef:
    return ServerFileRef("id", root + "/" + str(path.relative_to(music)).replace(os.sep, "/"), "t", "a", "b", "flac")


def findings(db: Database) -> dict[str, dict[str, Any]]:
    return {f["path"]: f for f in db.list_library_health_findings()}


def _make_record(
    server_id: str,
    name: str,
    kind: str = "plex",
    *,
    roles: frozenset[str] = frozenset(registry.ROLES),
    path_mapping: Optional[dict[str, Any]] = None,
) -> registry.ServerRecord:
    return registry.ServerRecord(
        id=server_id,
        kind=kind,
        name=name,
        source="settings",
        url=f"http://{server_id}",
        roles=roles,
        path_mapping=path_mapping,
    )


def test_two_servers_merge_findings_and_server_names(db: Database, music: Path) -> None:
    f_shared = touch(music / "Art/Alb/1.flac")
    f_both_miss = touch(music / "Art/Alb/2.flac")
    f_only_s2_miss = touch(music / "Art/Alb/3.flac")

    r1 = _make_record("s1", "Plex 1", "plex", path_mapping={"server_prefix": "/s1", "local_prefix": str(music), "auto": False})
    r2 = _make_record("s2", "Jellyfin 2", "jellyfin", path_mapping={"server_prefix": "/s2", "local_prefix": str(music), "auto": False})
    s1 = FakeServer([ref("/s1", music, f_shared), ref("/s1", music, f_only_s2_miss)], kind="plex")
    s2 = FakeServer([ref("/s2", music, f_shared)], kind="jellyfin")

    registry.install([r1, r2])
    with patch.object(registry, "build_adapter", side_effect=lambda r: s1 if r.id == "s1" else s2):
        run = lh.run_check(db, None, music_root=music)

    assert run["error"] is None
    assert run["server_kind"] == "Plex 1, Jellyfin 2"
    assert run["unindexed"] == 2
    got = findings(db)
    assert got[str(f_both_miss)]["detail"]["servers"] == ["Plex 1", "Jellyfin 2"]
    assert got[str(f_only_s2_miss)]["detail"]["servers"] == ["Jellyfin 2"]


def test_one_server_errors_other_continues(db: Database, music: Path) -> None:
    f_ok = touch(music / "Art/Alb/1.flac")
    f_miss = touch(music / "Art/Alb/2.flac")

    r1 = _make_record("s1", "Plex 1", "plex", path_mapping={"server_prefix": "/s1", "local_prefix": str(music), "auto": False})
    r2 = _make_record("s2", "Jellyfin 2", "jellyfin", path_mapping={"server_prefix": "/s2", "local_prefix": str(music), "auto": False})
    s1 = FakeServer([], error=MediaServerError("boom"), kind="plex")
    s2 = FakeServer([ref("/s2", music, f_ok)], kind="jellyfin")

    registry.install([r1, r2])
    with patch.object(registry, "build_adapter", side_effect=lambda r: s1 if r.id == "s1" else s2):
        run = lh.run_check(db, None, music_root=music)

    assert run["error"] is None
    assert run["errors"] == ["Plex 1: Could not read the media server library: MediaServerError"]
    assert run["server_kind"] == "Jellyfin 2"
    got = findings(db)
    assert str(f_miss) in got
    assert got[str(f_miss)]["detail"]["servers"] == ["Jellyfin 2"]


def test_per_server_mapping_and_auto_suggestion_saved_on_row(db: Database, music: Path) -> None:
    f1 = touch(music / "Art/Alb/1.flac")
    f2 = touch(music / "Art/Alb/2.flac")

    db.insert_media_server_row({"id": "s1", "kind": "plex", "name": "Plex 1", "url": "http://s1"})
    db.insert_media_server_row({"id": "s2", "kind": "jellyfin", "name": "Jellyfin 2", "url": "http://s2"})
    db.set_media_server_row_path_mapping(
        "s1", {"server_prefix": "/srv1", "local_prefix": str(music), "auto": False, "server_kind": "plex"}
    )

    r1 = _make_record("s1", "Plex 1", "plex")
    r2 = _make_record("s2", "Jellyfin 2", "jellyfin")
    s1 = FakeServer([ref("/srv1", music, f1), ref("/srv1", music, f2)], kind="plex")
    s2 = FakeServer([ref("/srv2", music, f1), ref("/srv2", music, f2)], kind="jellyfin")

    registry.install([r1, r2])
    with patch.object(registry, "build_adapter", side_effect=lambda r: s1 if r.id == "s1" else s2):
        run = lh.run_check(db, None, music_root=music)

    assert run["error"] is None
    assert run["unindexed"] == 0
    row2 = db.get_media_server_row("s2")
    assert row2 is not None
    assert row2["path_mapping"] == {
        "server_prefix": "/srv2",
        "local_prefix": str(music),
        "auto": True,
        "server_kind": "jellyfin",
    }


def test_skips_servers_without_refresh_role_or_file_paths(db: Database, music: Path) -> None:
    f1 = touch(music / "Art/Alb/1.flac")
    f2 = touch(music / "Art/Alb/2.flac")

    r_no_refresh = _make_record("s1", "NoRefresh", "plex", roles=frozenset({registry.ROLE_PLAYLISTS}))
    r_no_paths = _make_record("s2", "NoPaths", "subsonic")
    r_active = _make_record(
        "s3", "Active", "plex", path_mapping={"server_prefix": "/srv", "local_prefix": str(music), "auto": False}
    )

    s_no_refresh = FakeServer([], kind="plex")
    s_no_paths = FakeServer([], file_paths=False, kind="subsonic")
    s_active = FakeServer([ref("/srv", music, f1)], kind="plex")
    adapters = {"s1": s_no_refresh, "s2": s_no_paths, "s3": s_active}

    registry.install([r_no_refresh, r_no_paths, r_active])
    with patch.object(registry, "build_adapter", side_effect=lambda r: adapters[r.id]):
        run = lh.run_check(db, None, music_root=music)

    assert run["error"] is None
    assert run["server_kind"] == "Active"
    got = findings(db)
    assert got[str(f2)]["detail"]["servers"] == ["Active"]


def test_not_installed_keeps_legacy_behaviour(db: Database, music: Path) -> None:
    f1 = touch(music / "Art/Alb/1.flac")
    f2 = touch(music / "Art/Alb/2.flac")
    db.set_media_server_path_mapping(
        {"server_prefix": "/legacy", "local_prefix": str(music), "auto": False, "server_kind": "plex"}
    )
    srv = FakeServer([ref("/legacy", music, f1)], kind="plex")

    run = lh.run_check(db, srv, music_root=music)
    assert run["error"] is None
    assert run["server_kind"] == "plex"
    got = findings(db)
    assert got[str(f2)]["detail"]["servers"] == ["plex"]


def test_api_mappings_and_per_server_put_delete(db: Database, music: Path, tmp_path: Path) -> None:
    config = Config(plex_url="", plex_token="", data_dir=str(tmp_path))
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    user = db.upsert_user("a-1", "admin", "a@example.com", is_admin=True)
    token = create_session_token(user_id=user["id"], username="admin", is_admin=True, secret_key=secret)
    db.create_session(token, user["id"], {"auth": "test"})
    headers = {"Authorization": f"Bearer {token}"}

    db.insert_media_server_row({"id": "s1", "kind": "plex", "name": "Plex 1", "url": "http://s1"})
    db.insert_media_server_row({"id": "s2", "kind": "jellyfin", "name": "Jellyfin 2", "url": "http://s2"})
    r1 = _make_record("s1", "Plex 1", "plex")
    r2 = _make_record("s2", "Jellyfin 2", "jellyfin")
    s1 = FakeServer([], kind="plex")
    s2 = FakeServer([], kind="jellyfin")

    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_active_media_server] = lambda: s1
    client = TestClient(app)

    registry.install([r1, r2])
    with patch.object(registry, "build_adapter", side_effect=lambda r: s1 if r.id == "s1" else s2):
        res_404 = client.put(
            "/api/library-health/mapping",
            json={"server_id": "missing", "server_prefix": "/a", "local_prefix": "/b"},
            headers=headers,
        )
        assert res_404.status_code == 404

        res_put = client.put(
            "/api/library-health/mapping",
            json={"server_id": "s2", "server_prefix": "/jf", "local_prefix": str(music)},
            headers=headers,
        )
        assert res_put.status_code == 200
        assert res_put.json()["mappings"][1] == {
            "server_id": "s2",
            "name": "Jellyfin 2",
            "server_prefix": "/jf",
            "local_prefix": str(music),
            "auto": False,
        }

        overview = client.get("/api/library-health", headers=headers).json()
        assert len(overview["mappings"]) == 2
        assert overview["mappings"][1]["server_prefix"] == "/jf"

        res_del = client.delete("/api/library-health/mapping?server_id=s2", headers=headers)
        assert res_del.status_code == 200
        assert res_del.json()["mappings"][1]["server_prefix"] == ""
