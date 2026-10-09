"""Artist tags (migration v64): registry CRUD, artist assignment, bulk edit, tag-scoped delay and release profile
matching, import-list tag application, item history and the REST API."""

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import delay_gate
from trackseerr.acquisition_coordinator import AcquisitionCoordinator
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.clients.import_lists import ImportListItem
from trackseerr.config import Config
from trackseerr.item_history import GrabTrigger
from trackseerr.import_list_worker import sync_import_list
from trackseerr.models import AcquisitionSearchResult
from trackseerr.quality import parse_release_title
from trackseerr.storage import SCHEMA_VERSION, Database
from trackseerr.tag_store import DuplicateTag, InvalidTagLabel, UnknownTag, normalize_label
from tests.test_list_monitoring import ART, make_enricher


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr("trackseerr.mediacover.mediacover_service.ensure_artwork", lambda *a, **k: Path("/tmp/c.jpg"))


def artist(db: Database, aid: str, name: str = "") -> str:
    db.upsert_library_artist({"id": aid, "name": name or aid, "monitored": True, "monitor_option": "all"})
    return aid


# ------------------------------------------------------------------------------------------- labels


@pytest.mark.parametrize("raw,expected", [("  Metal ", "metal"), ("Post_Rock-90s", "post_rock-90s"), ("a b", "a b"), ("x" * 40, "x" * 40), ("R&B", "r&b"), (" Drum & Bass", "drum & bass")])
def test_normalize_label_ok(raw, expected):
    assert normalize_label(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "x" * 41, "rock!", "café", "a/b", None, 5])
def test_normalize_label_rejects(raw):
    with pytest.raises(InvalidTagLabel):
        normalize_label(raw)


def test_ampersand_label_registers_everywhere(db):
    assert db.create_tag("R&B")["label"] == "r&b"
    prof = db.create_release_profile({"name": "RB", "tags": ["R&B"]})
    assert prof["tags"] == ["r&b"]
    dp = _delay(db, "RB", ["r&b"])
    assert dp["tags"] == ["r&b"]
    lst = db.create_import_list({"name": "L", "provider": "lastfm", "config": {}, "tags": ["r&b"]})
    assert lst["tags"] == ["r&b"]


# ------------------------------------------------------------------------------------------- migration


def test_schema_version_and_tables(db):
    assert SCHEMA_VERSION >= 64
    names = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tags", "artist_tags"} <= names
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(import_lists)")}
    assert "tags_json" in cols


def test_migration_moves_legacy_tags_and_is_idempotent(tmp_path):
    path = str(tmp_path / "m.db")
    first = Database(path)
    first.create_delay_profile({
        "name": "Metal", "preferred_protocol": "usenet", "delays": {"usenet": 0, "torrent": 0, "soulseek": 0},
        "bypass_if_highest_quality": True, "tags": [],
    })
    first.create_release_profile({"name": "RP", "tags": []})
    first.upsert_library_artist({"id": "a1", "name": "Sepultura", "monitored": True, "monitor_option": "all"})
    first.upsert_library_artist({"id": "a2", "name": "Other", "monitored": True, "monitor_option": "all"})
    first.close()

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE artist_tags")
    conn.execute("DROP TABLE tags")
    conn.execute("DELETE FROM schema_migrations WHERE version >= 64")
    conn.execute("UPDATE delay_profiles SET tags_json = ? WHERE name = 'Metal'", (json.dumps(["Metal", " Rock!! ", "metal"]),))
    conn.execute("UPDATE release_profiles SET tags_json = ? WHERE name = 'RP'", (json.dumps(["Lossless"]),))
    conn.execute("UPDATE library_artists SET metadata_json = ? WHERE id = 'a1'", (json.dumps({"x": 1, "tags": ["Metal", "thrash"]}),))
    conn.execute("UPDATE library_artists SET metadata_json = ? WHERE id = 'a2'", ("{not json",))
    conn.commit()
    conn.close()

    for _ in range(2):  # the second open proves the migration is idempotent
        d = Database(path)
        labels = {t["label"] for t in d.list_tags()}
        assert labels == {"metal", "rock-", "lossless", "thrash"}
        profile = next(p for p in d.list_delay_profiles() if p["name"] == "Metal")
        assert profile["tags"] == ["metal", "rock-"]
        assert d.list_release_profiles()[-1]["tags"] == ["lossless"]
        assert d.get_artist_tag_labels(artist_id="a1") == ["metal", "thrash"]
        meta = json.loads(d.get_library_artist("a1")["metadata_json"])
        assert meta == {"x": 1}  # the metadata_json hack is gone
        assert d.get_library_artist("a2")["metadata_json"] == "{not json"  # unreadable data untouched
        d.close()


# ------------------------------------------------------------------------------------------- CRUD


def test_crud_and_validation(db):
    t = db.create_tag("  Metal ")
    assert t["label"] == "metal"
    with pytest.raises(DuplicateTag):
        db.create_tag("METAL")
    with pytest.raises(InvalidTagLabel):
        db.create_tag("no!")
    other = db.create_tag("rock")
    with pytest.raises(DuplicateTag):
        db.rename_tag(other["id"], "Metal")
    assert db.rename_tag(other["id"], "Hard Rock")["label"] == "hard rock"
    assert db.rename_tag(9999, "x") is None
    assert [x["label"] for x in db.list_tags()] == ["hard rock", "metal"]
    assert db.delete_tag(9999) is False


def test_usage_counts_and_rename_delete_rewrite_profiles(db):
    tag = db.create_tag("metal")
    artist(db, "a1")
    db.set_artist_tags("a1", [tag["id"]])
    db.create_delay_profile({"name": "D", "preferred_protocol": "usenet", "delays": {"usenet": 0, "torrent": 0, "soulseek": 0},
                             "bypass_if_highest_quality": True, "tags": ["metal", "jazz"]})
    db.create_release_profile({"name": "R", "tags": ["metal"]})
    db.create_import_list({"name": "L", "provider": "lastfm", "config": {}, "tags": ["metal"]})
    row = next(t for t in db.list_tags() if t["label"] == "metal")
    assert (row["artist_count"], row["delay_profile_count"], row["release_profile_count"], row["import_list_count"]) == (1, 1, 1, 1)
    assert {t["label"] for t in db.list_tags()} == {"metal", "jazz"}  # profile labels register themselves
    usage = db.get_tag_usage(tag["id"])
    assert [a["id"] for a in usage["artists"]] == ["a1"] and usage["delay_profiles"][0]["name"] == "D"

    db.rename_tag(tag["id"], "heavy")
    assert db.get_artist_tag_labels(artist_id="a1") == ["heavy"]
    assert next(p for p in db.list_delay_profiles() if p["name"] == "D")["tags"] == ["heavy", "jazz"]
    assert db.list_release_profiles()[-1]["tags"] == ["heavy"]
    assert db.list_import_lists()[0]["tags"] == ["heavy"]

    assert db.delete_tag(tag["id"]) is True
    assert db.get_artist_tag_labels(artist_id="a1") == []
    assert next(p for p in db.list_delay_profiles() if p["name"] == "D")["tags"] == ["jazz"]
    assert db.list_release_profiles()[-1]["tags"] == [] and db.list_import_lists()[0]["tags"] == []


def test_artist_tags_set_replace_and_cascade(db):
    a, b = db.create_tag("a")["id"], db.create_tag("b")["id"]
    artist(db, "x")
    assert db.set_artist_tags("x", [a, b, a]) == sorted([a, b], key=lambda i: i)
    assert db.set_artist_tags("x", [b]) == [b]
    assert db.set_artist_tags("nope", [a]) is None
    with pytest.raises(UnknownTag):
        db.set_artist_tags("x", [a, 4242])
    assert db.get_artist_tag_ids("x") == [b]  # a failed set changes nothing
    db.delete_library_artist("x")
    assert db.conn.execute("SELECT COUNT(*) FROM artist_tags").fetchone()[0] == 0  # artist delete cascades
    assert len(db.list_tags()) == 2  # the tags themselves survive


def test_tag_delete_cascades_assignments(db):
    t = db.create_tag("gone")
    artist(db, "x")
    db.set_artist_tags("x", [t["id"]])
    db.delete_tag(t["id"])
    assert db.conn.execute("SELECT COUNT(*) FROM artist_tags").fetchone()[0] == 0


def test_tags_changed_item_event(db):
    t = db.create_tag("metal")
    artist(db, "x")
    db.set_artist_tags("x", [t["id"]])
    db.set_artist_tags("x", [t["id"]])  # unchanged: no second event
    db.set_artist_tags("x", [])
    events = db.conn.execute("SELECT message, details_json FROM item_events WHERE event = 'tags_changed' ORDER BY id").fetchall()
    assert [json.loads(e["details_json"]) for e in events] == [
        {"added": ["metal"], "removed": []}, {"added": [], "removed": ["metal"]}
    ]


# ------------------------------------------------------------------------------------------- bulk


def test_bulk_add_remove_single_transaction(db):
    a, b = db.create_tag("a")["id"], db.create_tag("b")["id"]
    for i in ("x", "y", "z"):
        artist(db, i)
    db.set_artist_tags("z", [b])
    res = db.bulk_edit_artist_tags(["x", "y", "z"], add=[a], remove=[b])
    assert res == {"artists_updated": 3, "tags_added": 3, "tags_removed": 1}
    assert db.get_artist_tags_map(["x", "y", "z"]) == {"x": [a], "y": [a], "z": [a]}
    assert db.bulk_edit_artist_tags(["x", "y", "z"], add=[a])["tags_added"] == 0  # already there
    assert db.bulk_edit_artist_tags(None, remove=[a])["tags_removed"] == 3  # None = every artist
    with pytest.raises(ValueError):
        db.bulk_edit_artist_tags(["x"], add=[a], remove=[a])
    with pytest.raises(UnknownTag):
        db.bulk_edit_artist_tags(["x"], add=[a, 777])
    assert db.get_artist_tags_map(["x"]) == {}  # unknown id rolled nothing in


def test_bulk_edit_library_artists_combines_monitor_and_tags(db):
    a = db.create_tag("a")["id"]
    artist(db, "x")
    res = db.bulk_edit_library_artists(["x"], monitored=False, add_tag_ids=[a])
    assert res["tags_added"] == 1 and db.get_library_artist("x")["monitored"] is False
    res = db.bulk_edit_library_artists(["x"], remove_tag_ids=[a])
    assert res["tags_removed"] == 1 and db.get_artist_tag_ids("x") == []


# ------------------------------------------------------------------------------------------- delay profile matching


def _delay(db, name, tags, **kw):
    return db.create_delay_profile({"name": name, "preferred_protocol": kw.get("proto", "torrent"),
                                    "delays": {"usenet": 0, "torrent": 30, "soulseek": 0},
                                    "bypass_if_highest_quality": False, "tags": tags})


def test_delay_profile_selected_by_artist_tag(db):
    _delay(db, "Metal", ["metal"])
    artist(db, "a1", "Sepultura")
    artist(db, "a2", "Plain")
    db.set_artist_tags("a1", [db.get_tag_by_label("metal")["id"]])
    assert delay_gate.resolve_delay_profile(db, "Sepultura")["name"] == "Metal"  # by name
    assert delay_gate.resolve_delay_profile(db, None, "a1")["name"] == "Metal"  # by id
    assert delay_gate.resolve_delay_profile(db, "Plain")["is_default"]
    assert delay_gate.resolve_delay_profile(db, "Unknown Artist")["is_default"]
    assert delay_gate.artist_tags(db, "") == []


# ------------------------------------------------------------------------------------------- release profile scoping

HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4"


def _cand(title):
    return AcquisitionSearchResult(
        download_id=f"id-{title}", title=title, artist="Sepultura", album="Roots", size_bytes=300_000_000,
        magnet_url=f"magnet:?xt=urn:btih:{HASH}", source="torznab", protocol="torrent", seeders=10,
    )


def _rank(db, titles, tags):
    prof = db.get_default_quality_profile()
    return AcquisitionCoordinator().evaluate_and_rank([_cand(t) for t in titles], prof, db=db, artist_tags=tags)


def test_tagged_release_profile_applies_only_to_tagged_artists(db):
    db.create_release_profile({"name": "Ignore deluxe", "ignored": ["Deluxe"], "tags": ["metal"]})
    titles = ["Sepultura - Roots [FLAC]", "Sepultura - Roots Deluxe [FLAC]"]
    assert len(_rank(db, titles, ["metal"])) == 1  # tagged artist: the ignored term rejects the deluxe release
    assert len(_rank(db, titles, ["jazz"])) == 2  # other tag: profile does not apply
    assert len(_rank(db, titles, [])) == 2
    assert len(_rank(db, titles, None)) == 2


def test_untagged_release_profile_applies_to_everyone(db):
    db.create_release_profile({"name": "Ignore deluxe", "ignored": ["Deluxe"], "tags": []})
    titles = ["Sepultura - Roots [FLAC]", "Sepultura - Roots Deluxe [FLAC]"]
    for tags in (["metal"], [], None):
        assert len(_rank(db, titles, tags)) == 1


def test_search_and_grab_resolves_artist_tags_for_release_profiles(db):
    db.create_release_profile({"name": "Need remaster", "required": ["remaster"], "tags": ["metal"]})
    artist(db, "a1", "Sepultura")
    plain = [_cand("Sepultura - Roots [FLAC]")]
    coord = AcquisitionCoordinator()
    calls: list[Any] = []
    real = coord.evaluate_and_rank

    def spy(*a, **k):
        calls.append(k.get("artist_tags"))
        return real(*a, **k)

    def run():
        with patch.object(coord, "search_all_indexers", return_value=plain), patch.object(coord, "evaluate_and_rank", side_effect=spy):
            return coord.search_and_grab(artist="Sepultura", title="Roots", album="Roots", db=db, trigger=GrabTrigger("request"))

    run()
    db.set_artist_tags("a1", [db.get_tag_by_label("metal")["id"]])
    run()
    assert calls == [[], ["metal"]]  # the artist's tags reach the decision engine once it is tagged


# ------------------------------------------------------------------------------------------- import list tags


def test_import_list_tags_registered_and_validated(db):
    lst = db.create_import_list({"name": "L", "provider": "lastfm", "config": {}, "tags": ["Fresh", "fresh", "new"]})
    assert lst["tags"] == ["fresh", "new"]
    assert {t["label"] for t in db.list_tags()} == {"fresh", "new"}
    assert db.update_import_list(lst["id"], {"name": "L", "provider": "lastfm", "config": {}, "tags": ["x"]})["tags"] == ["x"]
    with pytest.raises(InvalidTagLabel):
        db.create_import_list({"name": "B", "provider": "lastfm", "config": {}, "tags": ["bad!"]})


def test_import_list_tags_applied_to_artist_it_adds_only(db):
    lst = db.create_import_list({
        "name": "L", "provider": "lastfm", "config": {"username": "u", "api_key": "k", "source": "loved_tracks"},
        "monitor_mode": "artist", "tags": ["discovered"],
    })
    item = ImportListItem(kind="artist", external_key=ART, artist_name="Radiohead", mbid=ART, artist_mbid=ART)
    with patch("trackseerr.import_list_worker.fetch_items", return_value=[item]):
        summary = sync_import_list(db, lst["id"], None, enricher=make_enricher())
    assert summary["applied"] == 1
    created = db.get_library_artist_by_mbid(ART)
    assert db.get_artist_tag_labels(artist_id=created["id"]) == ["discovered"]

    # An artist that already exists is left alone.
    db.set_artist_tags(created["id"], [])
    other = db.create_import_list({"name": "L2", "provider": "lastfm", "config": {"username": "u", "api_key": "k", "source": "loved_tracks"},
                                   "monitor_mode": "artist", "tags": ["again"]})
    with patch("trackseerr.import_list_worker.fetch_items", return_value=[item]):
        sync_import_list(db, other["id"], None, enricher=make_enricher())
    assert db.get_artist_tag_labels(artist_id=created["id"]) == []


# ------------------------------------------------------------------------------------------- API


@pytest.fixture
def api(db, tmp_path):
    cfg = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    client = TestClient(app)
    secret = get_or_create_secret_key(data_dir=cfg.data_dir)

    def headers(user):
        token = create_session_token(user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret)
        db.create_session(token, user["id"], {"auth": "test"})
        return {"Authorization": f"Bearer {token}"}

    admin = headers(db.upsert_user("admin-1", "admin_user", "a@x.tv", is_admin=True))
    alice = headers(db.upsert_user("alice-1", "alice", "al@x.tv", is_admin=False))
    return client, admin, alice, cfg


@pytest.mark.parametrize("method,path,payload", [
    ("get", "/api/tags", None), ("post", "/api/tags", {"label": "x"}), ("put", "/api/tags/1", {"label": "x"}),
    ("delete", "/api/tags/1", None), ("get", "/api/tags/1/usage", None),
    ("put", "/api/library/artists/a1/tags", {"tags": []}),
    ("post", "/api/library/artists/bulk-edit", {"artist_ids": ["a1"], "add_tags": [1]}),
])
def test_tag_routes_require_admin(api, method, path, payload):
    client, admin, alice, _ = api
    kw = {"json": payload} if payload is not None else {}
    assert getattr(client, method)(path, **kw).status_code == 401
    assert getattr(client, method)(path, headers=alice, **kw).status_code == 403


@pytest.mark.parametrize("method,path,payload", [
    ("get", "/api/tags", None), ("post", "/api/tags", {"label": "x"}), ("put", "/api/tags/1", {"label": "x"}),
    ("delete", "/api/tags/1", None), ("get", "/api/tags/1/usage", None),
    ("put", "/api/library/artists/a1/tags", {"tags": []}),
    ("post", "/api/library/artists/bulk-edit", {"artist_ids": ["a1"], "add_tags": [1]}),
])
def test_tag_routes_blocked_on_gateway(db, tmp_path, method, path, payload):
    """A real gateway-role app hides the tag routes exactly like every other core-only route: 404, never forwarded."""
    from trackseerr.clients.core_client import CoreClient

    cfg = Config(
        plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path), role="gateway",
        internal_core_secret="s" * 40, trackseerr_core_url="http://core.internal:5251",
    )
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    client = TestClient(app)
    user = db.upsert_user("admin-1", "admin_user", "a@x.tv", is_admin=True)
    secret = get_or_create_secret_key(data_dir=cfg.data_dir)
    token = create_session_token(user_id=user["id"], username=user["username"], is_admin=True, secret_key=secret)
    db.create_session(token, user["id"], {"auth": "test"})
    kw = {"json": payload} if payload is not None else {}
    with patch.object(CoreClient, "proxy") as proxy:
        res = client.request(method.upper(), path, headers={"Authorization": f"Bearer {token}"}, **kw)
    assert res.status_code == 404
    proxy.assert_not_called()


def test_tag_api_crud_flow(api, db):
    client, admin, _, _ = api
    r = client.post("/api/tags", json={"label": " Metal "}, headers=admin)
    assert r.status_code == 201 and r.json()["label"] == "metal" and r.json()["artist_count"] == 0
    tid = r.json()["id"]
    assert client.post("/api/tags", json={"label": "METAL"}, headers=admin).status_code == 409
    for bad in ("", "x" * 41, "no!", "é"):
        assert client.post("/api/tags", json={"label": bad}, headers=admin).status_code == 422
    assert client.put(f"/api/tags/{tid}", json={"label": "heavy"}, headers=admin).json()["label"] == "heavy"
    assert client.put("/api/tags/999", json={"label": "z"}, headers=admin).status_code == 404
    client.post("/api/tags", json={"label": "other"}, headers=admin)
    assert client.put(f"/api/tags/{tid}", json={"label": "other"}, headers=admin).status_code == 409
    assert [t["label"] for t in client.get("/api/tags", headers=admin).json()] == ["heavy", "other"]
    usage = client.get(f"/api/tags/{tid}/usage", headers=admin).json()
    assert usage["tag"]["id"] == tid and usage["artists"] == [] and usage["artist_count"] == 0
    assert client.get("/api/tags/999/usage", headers=admin).status_code == 404
    assert client.delete(f"/api/tags/{tid}", headers=admin).json() == {"status": "deleted", "id": tid}
    assert client.delete(f"/api/tags/{tid}", headers=admin).status_code == 404


def test_artist_tags_api_and_responses(api, db):
    client, admin, _, _ = api
    artist(db, "a1", "Sepultura")
    t1 = client.post("/api/tags", json={"label": "metal"}, headers=admin).json()["id"]
    t2 = client.post("/api/tags", json={"label": "thrash"}, headers=admin).json()["id"]
    r = client.put("/api/library/artists/a1/tags", json={"tags": [t2, t1]}, headers=admin)
    assert r.status_code == 200 and r.json() == {"artist_id": "a1", "tags": [t1, t2]}
    assert client.put("/api/library/artists/a1/tags", json={"tags": [999]}, headers=admin).status_code == 422
    assert client.put("/api/library/artists/nope/tags", json={"tags": []}, headers=admin).status_code == 404
    assert client.get("/api/library/artists/a1", headers=admin).json()["tags"] == [t1, t2]
    listed = client.get("/api/library/artists", headers=admin).json()
    assert next(a for a in listed if a["id"] == "a1")["tags"] == [t1, t2]
    usage = client.get(f"/api/tags/{t1}/usage", headers=admin).json()
    assert usage["artist_count"] == 1 and usage["artists"][0]["name"] == "Sepultura"


def test_bulk_edit_api_tags(api, db):
    client, admin, _, _ = api
    for i in ("a1", "a2"):
        artist(db, i)
    t = client.post("/api/tags", json={"label": "x"}, headers=admin).json()["id"]
    r = client.post("/api/library/artists/bulk-edit", json={"artist_ids": ["a1", "a2"], "add_tags": [t]}, headers=admin)
    assert r.status_code == 200 and r.json()["tags_added"] == 2
    r = client.post("/api/library/artists/bulk-edit", json={"artist_ids": ["a1"], "remove_tags": [t]}, headers=admin)
    assert r.json()["tags_removed"] == 1 and db.get_artist_tag_ids("a2") == [t]
    both = client.post("/api/library/artists/bulk-edit", json={"artist_ids": ["a1"], "add_tags": [t], "remove_tags": [t]}, headers=admin)
    assert both.status_code == 400
    assert client.post("/api/library/artists/bulk-edit", json={"artist_ids": ["a1"], "add_tags": [999]}, headers=admin).status_code == 400
    assert client.post("/api/library/artists/bulk-edit", json={"artist_ids": ["a1"]}, headers=admin).status_code == 400


def test_profile_and_list_apis_validate_and_register_tags(api, db):
    client, admin, _, _ = api
    r = client.post("/api/settings/delay-profiles", headers=admin, json={
        "name": "M", "preferred_protocol": "torrent", "delays": {"usenet": 0, "torrent": 5, "soulseek": 0}, "tags": ["Metal"]})
    assert r.status_code in (200, 201) and r.json()["tags"] == ["metal"]
    bad = client.post("/api/settings/delay-profiles", headers=admin, json={
        "name": "M", "preferred_protocol": "torrent", "delays": {"usenet": 0, "torrent": 5, "soulseek": 0}, "tags": ["bad!"]})
    assert bad.status_code == 422
    assert {t["label"] for t in client.get("/api/tags", headers=admin).json()} == {"metal"}


def test_head_health_excluded_from_openapi(api):
    client, _, _, _ = api
    paths = client.app.openapi()["paths"]
    for p in ("/api/health", "/api/health/ready"):
        assert client.head(p).status_code in (200, 503)
        assert p in paths and "get" in paths[p]
        assert "head" not in paths[p]


def test_interactive_search_passes_artist_tags_to_release_profiles(api, db):
    """POST /api/acquisition/search scopes tagged release profiles by the searched artist's tags."""
    client, admin, _, _ = api
    db.create_release_profile({"name": "Ignore deluxe", "ignored": ["Deluxe"], "tags": ["metal"]})
    artist(db, "a1", "Sepultura")
    db.set_artist_tags("a1", [db.get_tag_by_label("metal")["id"]])
    cands = [_cand("Sepultura - Roots Deluxe [FLAC]")]

    def search(artist_name):
        with patch("trackseerr.api.routes.acquisition.acquisition_coordinator.search_all_indexers", return_value=cands):
            res = client.post("/api/acquisition/search", headers=admin, json={"artist": artist_name, "album": "Roots", "item_type": "album"})
        assert res.status_code == 200
        return res.json()["results"][0]

    tagged = search("Sepultura")
    assert tagged["is_acceptable"] is False
    assert any("deluxe" in r.lower() for r in tagged["rejection_reasons"])
    assert search("Someone Else")["is_acceptable"] is True  # untagged / unknown artist: the tagged profile does not apply


# ------------------------------------------------------------------------------------------- atomic registration


def _tag_labels(db):
    return {t["label"] for t in db.list_tags()}


def test_update_missing_release_profile_registers_no_tags(db):
    assert db.update_release_profile(999, {"name": "Ghost", "tags": ["orphan"]}) is None
    assert _tag_labels(db) == set()


def test_update_default_delay_profile_registers_no_tags(db):
    default = next(p for p in db.list_delay_profiles() if p["is_default"])
    out = db.update_delay_profile(default["id"], {
        "name": default["name"], "preferred_protocol": "usenet", "delays": {"usenet": 0, "torrent": 0, "soulseek": 0},
        "bypass_if_highest_quality": True, "tags": ["orphan"],
    })
    assert out["tags"] == []
    assert _tag_labels(db) == set()
    assert db.update_delay_profile(9999, {
        "name": "x", "preferred_protocol": "usenet", "delays": {"usenet": 0, "torrent": 0, "soulseek": 0},
        "bypass_if_highest_quality": True, "tags": ["orphan"],
    }) is None
    assert _tag_labels(db) == set()


def test_invalid_label_registers_nothing(db):
    with pytest.raises(InvalidTagLabel):
        db.create_release_profile({"name": "Bad", "tags": ["fine", "no!"]})
    assert _tag_labels(db) == set()


def test_failed_release_profile_write_leaves_no_new_tags(db):
    db.create_release_profile({"name": "Taken", "tags": []})
    other = db.create_release_profile({"name": "Other", "tags": []})
    with pytest.raises(sqlite3.IntegrityError):
        db.create_release_profile({"name": "Taken", "tags": ["new-a"]})  # UNIQUE name
    with pytest.raises(sqlite3.IntegrityError):
        db.update_release_profile(other["id"], {"name": "Taken", "tags": ["new-b"]})
    assert _tag_labels(db) == set()
    # the connection is usable afterwards and a later commit does not resurrect the rolled-back tags
    db.create_tag("later")
    assert _tag_labels(db) == {"later"}


def test_failed_delay_profile_write_leaves_no_new_tags(db):
    bad = {"name": "D", "preferred_protocol": "torrent", "delays": {"usenet": 0, "torrent": "x", "soulseek": 0},
           "bypass_if_highest_quality": False, "tags": ["new-d"]}
    with pytest.raises(ValueError):
        db.create_delay_profile(bad)
    created = _delay(db, "Fine", [])
    with pytest.raises(ValueError):
        db.update_delay_profile(created["id"], bad)
    assert _tag_labels(db) == set()


def test_failed_import_list_write_leaves_no_new_tags(db):
    with pytest.raises(TypeError):
        db.create_import_list({"name": "L", "provider": "lastfm", "config": {"x": object()}, "tags": ["new-l"]})
    lst = db.create_import_list({"name": "L2", "provider": "lastfm", "config": {}, "tags": []})
    with pytest.raises(TypeError):
        db.update_import_list(lst["id"], {"name": "L2", "provider": "lastfm", "config": {"x": object()}, "tags": ["new-m"]})
    assert _tag_labels(db) == set()
    assert db.update_import_list("missing", {"name": "L", "provider": "lastfm", "config": {}, "tags": ["new-n"]}) is None
    assert _tag_labels(db) == set()


# ------------------------------------------------------------------------------------------- artist tags on every evaluation path


def test_backlog_artist_tag_lookup_is_cached_per_artist(db):
    from trackseerr.backlog_worker import _cached_artist_tags

    db.create_tag("metal")
    artist(db, "a1", "Sepultura")
    db.set_artist_tags("a1", [db.get_tag_by_label("metal")["id"]])
    cache: dict[str, list[str]] = {}
    with patch("trackseerr.backlog_worker.delay_gate.artist_tags", wraps=delay_gate.artist_tags) as spy:
        assert _cached_artist_tags(db, cache, "Sepultura") == ["metal"]
        assert _cached_artist_tags(db, cache, " sepultura ") == ["metal"]
        assert _cached_artist_tags(db, cache, "") == []
    assert spy.call_count == 1


def test_current_floor_passes_tags_to_evaluate_release(db):
    from trackseerr.acquisition_coordinator import _to_quality_profile
    from trackseerr import quality
    from trackseerr.backlog_worker import _current_floor

    prof = _to_quality_profile(db.get_default_quality_profile())
    with patch("trackseerr.backlog_worker.evaluate_release", wraps=quality.evaluate_release) as ev:
        _current_floor(db, prof, "FLAC", request_id="r1", artist_tags=["metal"])
    assert ev.call_args.kwargs["artist_tags"] == ["metal"]


def test_library_scanner_evaluates_with_artist_tags_once_per_artist(db, tmp_path):
    from trackseerr.library_scanner import LibraryScanner

    album_dir = tmp_path / "music" / "Sepultura" / "Roots"
    album_dir.mkdir(parents=True)
    meta = {}
    for n in (1, 2):
        f = album_dir / f"0{n} - Track {n}.flac"
        f.write_bytes(b"x")
        meta[str(f.resolve())] = {
            "title": f"Track {n}", "artist": "Sepultura", "album": "Roots", "track_number": n, "disc_number": 1,
            "codec": "FLAC", "bitrate": 900000, "sample_rate": 44100, "bits_per_sample": 16,
            "quality_full": "FLAC 16bit 44.1kHz", "file_path": str(f.resolve()),
        }
    artist(db, "a1", "Sepultura")
    db.set_artist_tags("a1", [db.create_tag("metal")["id"]])
    from trackseerr import quality

    with patch("trackseerr.library_scanner.inspect_audio_file", side_effect=lambda p: meta[str(Path(p).resolve())]), \
         patch("trackseerr.library_scanner.evaluate_release", wraps=quality.evaluate_release) as ev, \
         patch("trackseerr.library_scanner.delay_gate.artist_tags", wraps=delay_gate.artist_tags) as lookup:
        res = LibraryScanner().scan(db, root_folder=str(tmp_path / "music"))
    assert res["status"] == "completed"
    assert ev.call_count == 2
    assert all(c.kwargs["artist_tags"] == ["metal"] for c in ev.call_args_list)
    assert lookup.call_count == 1  # once per artist, not per file
