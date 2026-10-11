"""Search log: migration v74, store, and GET /api/activity/searches."""

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.storage import Database


@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db
    db.close()


def _rec(db, **kw):
    base = dict(source_name="Prowlarr", source_type="torznab", query_artist="A", query_title="T", result_count=2)
    base.update(kw)
    db.record_search(**base)


def test_migration_v74_creates_search_log_and_remote_state(tmp_path):
    path = str(tmp_path / "m.db")
    d = Database(path)
    d.conn.execute("DROP TABLE search_log")
    d.conn.execute("ALTER TABLE active_downloads DROP COLUMN remote_state")
    d.conn.execute("UPDATE schema_migrations SET version = 73 WHERE version = 74")
    d.conn.commit()
    d.close()
    d2 = Database(path)
    try:
        assert d2.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 74
        assert d2.conn.execute("SELECT COUNT(*) FROM search_log").fetchone()[0] == 0
        cols = {r[1] for r in d2.conn.execute("PRAGMA table_info(active_downloads)")}
        assert "remote_state" in cols
    finally:
        d2.close()


def test_record_and_list_newest_first(test_db):
    _rec(test_db, query_title="first", trigger_kind="manual")
    _rec(test_db, query_title="second", rung=2, error="boom")
    rows, total = test_db.list_search_log(1, 50)
    assert total == 2
    assert [r["query_title"] for r in rows] == ["second", "first"]
    assert rows[0]["rung"] == 2 and rows[0]["error"] == "boom"
    assert rows[1]["trigger_kind"] == "manual"
    page2, _ = test_db.list_search_log(2, 1)
    assert [r["query_title"] for r in page2] == ["first"]


def test_list_filters_source_type_and_request(test_db):
    _rec(test_db, source_type="slskd", request_id="r1")
    _rec(test_db, source_type="torznab", request_id="r1")
    _rec(test_db, source_type="torznab", request_id="r2")
    assert test_db.list_search_log(1, 50, source_type="slskd")[1] == 1
    assert test_db.list_search_log(1, 50, request_id="r1")[1] == 2
    assert test_db.list_search_log(1, 50, source_type="torznab", request_id="r2")[1] == 1


def test_record_prunes_older_than_30_days(test_db):
    _rec(test_db, query_title="old")
    test_db.conn.execute("UPDATE search_log SET created_at = datetime('now', '-31 days')")
    test_db.conn.commit()
    _rec(test_db, query_title="new")
    rows, total = test_db.list_search_log(1, 50)
    assert total == 1 and rows[0]["query_title"] == "new"


def test_activity_searches_endpoint_admin_only_and_paged(test_db, tmp_path):
    config = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
    app = create_app(db=test_db, config=config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: config
    api = TestClient(app)
    admin = test_db.upsert_user("admin-1", "admin_user", "a@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "al@plex.tv", is_admin=False)
    secret = get_or_create_secret_key(data_dir=config.data_dir)

    def headers(user):
        token = create_session_token(
            user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret
        )
        test_db.create_session(token, user["id"], {"auth": "test"})
        return {"Authorization": f"Bearer {token}"}

    for i in range(3):
        _rec(test_db, query_title=f"t{i}", source_type="slskd" if i == 0 else "torznab")

    assert api.get("/api/activity/searches", headers=headers(alice)).status_code == 403
    res = api.get("/api/activity/searches?page=1&page_size=2", headers=headers(admin))
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 3 and body["page_size"] == 2 and len(body["records"]) == 2
    assert body["records"][0]["query_title"] == "t2"
    only = api.get("/api/activity/searches?source_type=slskd", headers=headers(admin)).json()
    assert only["total"] == 1
    assert api.get("/api/activity/searches?source_type=bogus", headers=headers(admin)).status_code == 422
