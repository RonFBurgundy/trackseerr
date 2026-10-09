"""Per-indexer seed rules: migration, CRUD, grab-time snapshot, qBittorrent share limits, governance, seeders."""
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from trackseerr import activity_service as svc
from trackseerr.acquisition_coordinator import _to_quality_profile
from trackseerr.acquisition_worker import settle_transfer_after_import
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.clients.acquisition import base as acq_base
from trackseerr.clients.acquisition.base import AcquisitionDriver
from trackseerr.clients.acquisition.qbittorrent import QbittorrentDriver
from trackseerr.config import Config
from trackseerr.decision_engine import candidate_context, evaluate_prepared, prepare_profile
from trackseerr.models import (
    AcquisitionSearchResult,
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    IndexerConfig,
)
from trackseerr.quality import parse_release_title
from trackseerr.seed_rules import (
    apply_seed_rules_at_grab,
    is_discography_release,
    resolve_seed_targets,
    seed_rule_conflict,
)
from trackseerr.security import mask_secret
from trackseerr.storage import SCHEMA_VERSION, Database


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "seed.db"))
    yield d
    d.close()


@pytest.fixture
def config(tmp_path: Path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


@pytest.fixture
def headers(db: Database, config: Config) -> dict[str, str]:
    admin = db.upsert_user("admin-1", "admin_user", "admin@example.com", is_admin=True)
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=admin["id"], username="admin_user", is_admin=True, secret_key=secret)
    db.create_session(token, admin["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(db: Database, config: Config):
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


def _indexer(db: Database, iid: str = "ix1", **kw: Any) -> dict[str, Any]:
    return db.create_indexer(IndexerConfig(id=iid, name=f"Idx {iid}", indexer_type=kw.pop("indexer_type", "torznab"),
                                           host_url="http://192.168.1.5:9696/api", **kw))


def _download(db: Database, did: str = "dl-1", **kw: Any) -> None:
    if not db.get_download_client("c1"):
        db.create_download_client(DownloadClientConfig(id="c1", name="qB", driver_type=DownloadDriverType.QBITTORRENT,
                                                       host_url="http://qbit:8080"))
    db.create_active_download(ActiveDownload(id=did, client_id="c1", title="A - B [FLAC]", artist="A", item_type="album",
                                             status=DownloadStatus.COMPLETED.value, download_hash="abc"))


# ------------------------------------------------------------------ migration


def test_migration_v56_columns(db: Database):
    assert SCHEMA_VERSION >= 56
    assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
    idx_cols = {r[1] for r in db.conn.execute("PRAGMA table_info(indexers)")}
    assert {"seed_ratio", "seed_time_minutes", "discography_seed_time_minutes", "minimum_seeders"} <= idx_cols
    dl_cols = {r[1] for r in db.conn.execute("PRAGMA table_info(active_downloads)")}
    assert {"indexer_id", "seed_ratio_target", "seed_time_target_minutes", "seed_rule_source"} <= dl_cols


def test_migration_v56_idempotent_on_existing_columns(db: Database):
    cur = db.conn.cursor()
    db._migration_v56(cur)  # re-running must not raise on already-present columns
    assert _indexer(db)["seed_ratio"] is None


# ------------------------------------------------------------------ indexer CRUD + validation


def test_indexer_api_round_trip_and_null_inherit(client: TestClient, headers: dict[str, str]):
    payload = {"name": "Tracker", "indexer_type": "torznab", "host_url": "http://192.168.1.100:9696/1/api",
               "seed_ratio": 1.5, "seed_time_minutes": 4320, "discography_seed_time_minutes": 10080,
               "minimum_seeders": 3}
    created = client.post("/api/settings/indexers", json=payload, headers=headers)
    assert created.status_code == 200, created.text
    body = created.json()
    assert (body["seed_ratio"], body["seed_time_minutes"], body["discography_seed_time_minutes"],
            body["minimum_seeders"]) == (1.5, 4320, 10080, 3)
    assert body["indexer_type"] == "torznab"
    listed = client.get("/api/settings/indexers", headers=headers).json()
    assert listed[0]["seed_ratio"] == 1.5 and listed[0]["minimum_seeders"] == 3
    # Omitted fields round-trip as null (inherit global); 0 is preserved (no requirement).
    upd = client.post("/api/settings/indexers", json={**payload, "id": body["id"], "seed_ratio": 0,
                                                       "seed_time_minutes": None}, headers=headers).json()
    assert upd["seed_ratio"] == 0 and upd["seed_time_minutes"] is None


@pytest.mark.parametrize("field,value", [("seed_ratio", -0.1), ("seed_time_minutes", -1),
                                         ("discography_seed_time_minutes", -5), ("minimum_seeders", -1),
                                         ("seed_time_minutes", 1.5)])
def test_indexer_api_rejects_invalid_seed_values(client: TestClient, headers: dict[str, str], field: str, value: Any):
    payload = {"name": "T", "host_url": "http://192.168.1.100:9696/1/api", field: value}
    assert client.post("/api/settings/indexers", json=payload, headers=headers).status_code == 422


# ------------------------------------------------------------------ resolution + grab-time snapshot


GLOBALS = {"seed_ratio_limit": 2.0, "seed_time_limit_minutes": 100}


def test_resolve_indexer_override_and_inherit():
    assert resolve_seed_targets({"seed_ratio": 1.0, "seed_time_minutes": 60}, GLOBALS) == (1.0, 60, "indexer")
    # Partial override: ratio from indexer, time inherited from global.
    assert resolve_seed_targets({"seed_ratio": 1.0}, GLOBALS) == (1.0, 100, "indexer")
    assert resolve_seed_targets({}, GLOBALS) == (2.0, 100, "global")
    assert resolve_seed_targets(None, {"seed_ratio_limit": None, "seed_time_limit_minutes": None}) == (None, None, None)
    # 0 = no requirement, still an indexer override
    assert resolve_seed_targets({"seed_ratio": 0}, GLOBALS) == (0.0, 100, "indexer")


def test_resolve_discography_uses_discography_time():
    idx = {"seed_time_minutes": 60, "discography_seed_time_minutes": 500}
    assert resolve_seed_targets(idx, GLOBALS, is_discography=True)[1] == 500
    assert resolve_seed_targets(idx, GLOBALS, is_discography=False)[1] == 60
    # Unset discography time falls back to the normal time
    assert resolve_seed_targets({"seed_time_minutes": 60}, GLOBALS, is_discography=True)[1] == 60
    # Discography field alone does not make a normal grab an indexer-sourced rule
    assert resolve_seed_targets({"discography_seed_time_minutes": 500}, GLOBALS, False) == (2.0, 100, "global")


def test_discography_detection_from_title():
    assert is_discography_release("Artist - Discography (1990-2010) [FLAC]")
    assert is_discography_release("Artist - Complete Studio Albums FLAC")
    assert not is_discography_release("Artist - Discovery [FLAC]")
    assert is_discography_release("x", {"is_discography": True})


def test_grab_snapshot_indexer_override(db: Database):
    _indexer(db, seed_ratio=1.0, seed_time_minutes=4320)
    db.update_media_management_settings(GLOBALS)
    _download(db)
    driver = MagicMock()
    apply_seed_rules_at_grab(db, driver, "dl-1", "abc", "A - B [FLAC]", "torrent", {"indexer_id": "ix1"})
    row = db.get_active_download("dl-1")
    assert (row["indexer_id"], row["seed_ratio_target"], row["seed_time_target_minutes"], row["seed_rule_source"]) == (
        "ix1", 1.0, 4320, "indexer")
    driver.set_share_limits.assert_called_once_with("abc", 1.0, 4320)


def test_grab_snapshot_inherits_global_and_does_not_push(db: Database):
    _indexer(db)
    db.update_media_management_settings(GLOBALS)
    _download(db)
    driver = MagicMock()
    apply_seed_rules_at_grab(db, driver, "dl-1", "abc", "A - B", "torrent", {"indexer_id": "ix1"})
    row = db.get_active_download("dl-1")
    assert (row["seed_ratio_target"], row["seed_time_target_minutes"], row["seed_rule_source"]) == (2.0, 100, "global")
    driver.set_share_limits.assert_not_called()


def test_grab_snapshot_discography_and_later_edit_is_not_retroactive(db: Database):
    _indexer(db, seed_time_minutes=60, discography_seed_time_minutes=900)
    _download(db)
    apply_seed_rules_at_grab(db, MagicMock(), "dl-1", "abc", "Artist - Discography FLAC", "torrent", {"indexer_id": "ix1"})
    db.update_indexer("ix1", {"seed_time_minutes": 1})
    assert db.get_active_download("dl-1")["seed_time_target_minutes"] == 900


def test_grab_snapshot_skips_usenet_and_survives_push_failure(db: Database):
    _indexer(db, seed_ratio=1.0)
    _download(db)
    apply_seed_rules_at_grab(db, MagicMock(), "dl-1", "abc", "t", "usenet", {"indexer_id": "ix1"})
    assert db.get_active_download("dl-1")["seed_rule_source"] is None
    boom = MagicMock()
    boom.set_share_limits.side_effect = RuntimeError("client down")
    apply_seed_rules_at_grab(db, boom, "dl-1", "abc", "t", "torrent", {"indexer_id": "ix1"})  # must not raise
    assert db.get_active_download("dl-1")["seed_rule_source"] == "indexer"


# ------------------------------------------------------------------ client share limits


def _mock_http(status: int = 200):
    resp = MagicMock(status_code=status, text="Ok.", headers={})
    http = MagicMock()
    http.__enter__.return_value = http
    http.post.return_value = resp
    http.headers = {}
    return http


def _push(ratio, minutes, status: int = 200):
    http = _mock_http(status)
    drv = QbittorrentDriver("http://192.168.1.9:8080")
    with patch("trackseerr.clients.acquisition.qbittorrent.httpx.Client", return_value=http):
        ok = drv.set_share_limits("ABCDEF", ratio, minutes)
    return ok, http


def test_qbittorrent_set_share_limits_params():
    ok, http = _push(1.5, 4320)
    assert ok is True
    url = [c for c in http.post.call_args_list if "setShareLimits" in c.args[0]][0]
    assert url.args[0].endswith("/api/v2/torrents/setShareLimits")
    assert url.kwargs["data"] == {"hashes": "abcdef", "ratioLimit": 1.5, "seedingTimeLimit": 4320,
                                  "inactiveSeedingTimeLimit": -2}


@pytest.mark.parametrize("ratio,minutes,exp_r,exp_t", [(None, None, -2, -2), (0, 0, -1, -1), (2.0, None, 2.0, -2),
                                                       (None, 90, -2, 90)])
def test_qbittorrent_share_limit_sentinels(ratio, minutes, exp_r, exp_t):
    _, http = _push(ratio, minutes)
    data = [c for c in http.post.call_args_list if "setShareLimits" in c.args[0]][0].kwargs["data"]
    assert (data["ratioLimit"], data["seedingTimeLimit"]) == (exp_r, exp_t)


def test_qbittorrent_share_limits_failures_return_false():
    assert _push(1.0, 10, status=500)[0] is False
    http = _mock_http()
    http.post.side_effect = httpx.ConnectError("down")
    with patch("trackseerr.clients.acquisition.qbittorrent.httpx.Client", return_value=http):
        assert QbittorrentDriver("http://192.168.1.9:8080").set_share_limits("a", 1.0, 1) is False


def test_unsupported_driver_returns_false_and_logs_once(caplog):
    class Dummy(AcquisitionDriver):
        def test_connection(self): return True, ""
        def search(self, artist, title=None, album=None): return []
        def download(self, result): return "x"
        def get_status(self, download_id): return {}
        def cancel(self, download_id): return True

    acq_base._share_limits_unsupported_logged.discard("Dummy")
    with caplog.at_level("INFO", logger=acq_base.logger.name):
        assert Dummy().set_share_limits("h", 1.0, 5) is False
        assert Dummy().set_share_limits("h", 1.0, 5) is False
    assert sum("support share limits" in r.message for r in caplog.records) == 1


# ------------------------------------------------------------------ governance


MS_ON = {"seed_complete_action": "remove", "seed_ratio_limit": None, "seed_time_limit_minutes": None}


def _row(source="indexer", ratio=1.0, minutes=60):
    return {"seed_rule_source": source, "seed_ratio_target": ratio, "seed_time_target_minutes": minutes}


def _st(ratio=0.0, secs=0):
    return {"status": "completed", "ratio": ratio, "seeding_time_seconds": secs}


@pytest.mark.parametrize("mode", ["move", "hardlink", "copy"])
def test_indexer_rule_unmet_never_cleans_up(mode):
    driver = MagicMock()
    out = settle_transfer_after_import(driver, "h", MS_ON, mode, _st(0.5, 600), _row())
    assert out == DownloadStatus.COMPLETED.value
    driver.cleanup_completed.assert_not_called()


def test_indexer_rule_status_unavailable_keeps():
    driver = MagicMock()
    assert settle_transfer_after_import(driver, "h", MS_ON, "move", None, _row()) == DownloadStatus.COMPLETED.value
    driver.cleanup_completed.assert_not_called()


def test_indexer_rule_ratio_met_cleans_up_without_deleting_files():
    driver = MagicMock()
    out = settle_transfer_after_import(driver, "h", MS_ON, "hardlink", _st(1.2, 60), _row())
    assert out == DownloadStatus.IMPORTED.value
    driver.cleanup_completed.assert_called_once_with("h", delete_files=False)


def test_indexer_rule_time_met_cleans_up():
    driver = MagicMock()
    out = settle_transfer_after_import(driver, "h", MS_ON, "copy", _st(0.1, 3600), _row())
    assert out == DownloadStatus.IMPORTED.value
    driver.cleanup_completed.assert_called_once_with("h", delete_files=False)


def test_indexer_rule_zero_target_is_not_met_on_its_own():
    driver = MagicMock()
    # ratio-only rule (time 0): lots of seeding time must not satisfy it
    out = settle_transfer_after_import(driver, "h", MS_ON, "copy", _st(0.2, 10**6), _row(ratio=1.0, minutes=0))
    assert out == DownloadStatus.COMPLETED.value
    driver.cleanup_completed.assert_not_called()


def test_indexer_rule_all_zero_means_no_requirement():
    driver = MagicMock()
    out = settle_transfer_after_import(driver, "h", MS_ON, "copy", _st(), _row(ratio=0, minutes=0))
    assert out == DownloadStatus.IMPORTED.value
    driver.cleanup_completed.assert_called_once()


def test_indexer_rule_with_delete_completed_off_does_nothing():
    driver = MagicMock()
    ms = {**MS_ON, "seed_complete_action": "keep"}
    assert settle_transfer_after_import(driver, "h", ms, "move", _st(5, 10**6), _row()) == DownloadStatus.IMPORTED.value
    driver.cleanup_completed.assert_not_called()


def test_legacy_row_uses_global_limits_unchanged():
    ms = {"seed_complete_action": "remove", "seed_ratio_limit": 1.0, "seed_time_limit_minutes": None}
    d1 = MagicMock()
    legacy = {"seed_rule_source": None, "seed_ratio_target": 99.0}  # stale snapshot values are ignored without a source
    assert settle_transfer_after_import(d1, "h", ms, "hardlink", _st(0.5), legacy) == DownloadStatus.COMPLETED.value
    d1.cleanup_completed.assert_not_called()
    d2 = MagicMock()
    assert settle_transfer_after_import(d2, "h", ms, "hardlink", _st(1.5), None) == DownloadStatus.IMPORTED.value
    d2.cleanup_completed.assert_called_once_with("h", delete_files=False)
    d3 = MagicMock()  # move mode with globals still cleans up immediately (today's behaviour)
    assert settle_transfer_after_import(d3, "h", ms, "move", _st(0.0), None) == DownloadStatus.IMPORTED.value


def test_global_snapshot_governs_like_globals():
    d = MagicMock()
    ms = {"seed_complete_action": "remove", "seed_ratio_limit": 99.0, "seed_time_limit_minutes": None}
    row = _row(source="global", ratio=1.0, minutes=None)
    assert settle_transfer_after_import(d, "h", ms, "hardlink", _st(1.1), row) == DownloadStatus.IMPORTED.value


# ------------------------------------------------------------------ minimum seeders


def _evaluate(title_seeders, minimum, protocol="torrent"):
    profile = _to_quality_profile({"id": "p1", "name": "P", "cutoff": "FLAC 16bit",
                                   "items": [{"type": "quality", "quality": "FLAC 16bit", "allowed": True}],
                                   "format_items": [], "catalog": {"definitions": [], "formats": [], "release_profiles": []}})
    cand = AcquisitionSearchResult(download_id="d", title="A - B [FLAC 16bit]", artist="A", seeders=title_seeders,
                                   protocol=protocol, source=protocol,
                                   extra={"indexer_id": "ix1", "indexer_minimum_seeders": minimum})
    return evaluate_prepared(parse_release_title(cand.title), prepare_profile(profile), None, **candidate_context(cand))


def test_minimum_seeders_rejection():
    res = _evaluate(2, 5)
    assert not res.is_acceptable
    assert any(r["code"] == "seeders_below_min" for r in res.breakdown.rejections)
    assert _evaluate(5, 5).is_acceptable
    assert _evaluate(0, None).breakdown.rejections and not _evaluate(0, None).is_acceptable  # default minimum 1
    assert _evaluate(1, None).is_acceptable
    assert _evaluate(0, 0).is_acceptable  # indexer override: no requirement
    assert _evaluate(None, 5).is_acceptable  # unknown seeder count is never rejected
    assert _evaluate(0, 5, protocol="usenet").is_acceptable


# ------------------------------------------------------------------ seed_rule_conflict


def test_seed_rule_conflict_truth_table():
    ix = lambda **k: {"enabled": True, "indexer_type": "torznab", **k}  # noqa: E731
    assert seed_rule_conflict("move", [ix(seed_ratio=1.0)])
    assert seed_rule_conflict("move", [ix(seed_time_minutes=60)])
    assert seed_rule_conflict("move", [ix(discography_seed_time_minutes=60)])
    assert not seed_rule_conflict("hardlink", [ix(seed_ratio=1.0)])
    assert not seed_rule_conflict("copy", [ix(seed_ratio=1.0)])
    assert not seed_rule_conflict("move", [ix()])
    assert not seed_rule_conflict("move", [ix(seed_ratio=0, seed_time_minutes=0)])
    assert not seed_rule_conflict("move", [ix(seed_ratio=1.0, enabled=False)])
    assert not seed_rule_conflict("move", [ix(seed_ratio=1.0, indexer_type="newznab")])
    assert not seed_rule_conflict("move", [])


def test_media_management_get_exposes_seed_rule_conflict(client: TestClient, headers: dict[str, str], db: Database):
    db.update_media_management_settings({"import_mode": "move"})
    assert client.get("/api/settings/media-management", headers=headers).json()["seed_rule_conflict"] is False
    _indexer(db, seed_ratio=1.0)
    assert client.get("/api/settings/media-management", headers=headers).json()["seed_rule_conflict"] is True
    db.update_media_management_settings({"import_mode": "hardlink"})
    assert client.get("/api/settings/media-management", headers=headers).json()["seed_rule_conflict"] is False


# ------------------------------------------------------------------ queue seeding field


def test_queue_seeding_field(db: Database):
    _indexer(db, seed_ratio=1.0, seed_time_minutes=4320)
    _download(db)
    db.set_download_release_meta("dl-1", protocol="torrent")
    db.set_download_seed_rule("dl-1", "ix1", 1.0, 4320, "indexer")
    db.record_seed_progress("dl-1", 0.62, 31 * 3600)
    rec = svc.native_queue(db, 1, 25, "added_at", "desc")["records"][0]
    assert rec["seeding"] == {"ratio": 0.62, "ratio_target": 1.0, "seeding_minutes": 31 * 60, "time_target_minutes": 4320,
                              "removes_in_minutes": None, "action": "keep"}


def test_queue_seeding_null_for_non_seeding_rows(db: Database):
    _download(db)
    db.set_download_release_meta("dl-1", protocol="usenet")
    assert svc.native_queue(db, 1, 25, "added_at", "desc")["records"][0]["seeding"] is None
    db.set_download_release_meta("dl-1", protocol="torrent")
    db.update_download_status("dl-1", status="downloading")
    assert svc.native_queue(db, 1, 25, "added_at", "desc")["records"][0]["seeding"] is None


def test_queue_seeding_legacy_row_uses_global_targets(db: Database):
    db.update_media_management_settings({"seed_ratio_limit": 2.0, "seed_time_limit_minutes": 120})
    _download(db)
    db.set_download_release_meta("dl-1", protocol="torrent")
    s = svc.native_queue(db, 1, 25, "added_at", "desc")["records"][0]["seeding"]
    assert s == {"ratio": 0.0, "ratio_target": 2.0, "seeding_minutes": 0, "time_target_minutes": 120,
                 "removes_in_minutes": None, "action": "keep"}


# ------------------------------------------------------------------ indexer test route key resolution


def test_indexer_test_route_uses_stored_key_for_masked_key(client: TestClient, headers: dict[str, str], db: Database):
    _indexer(db, api_key="real-secret")
    driver = MagicMock()
    driver.test_connection.return_value = (True, "ok")
    with patch("trackseerr.api.routes.indexers.get_indexer_driver", return_value=driver) as gid:
        for key in (mask_secret("real-secret"), ""):
            resp = client.post("/api/settings/indexers/test", headers=headers,
                               json={"id": "ix1", "host_url": "http://192.168.1.5:9696/api", "api_key": key})
            assert resp.status_code == 200 and resp.json()["success"] is True
            assert gid.call_args.args[0]["api_key"] == "real-secret"


def test_indexer_test_route_never_sends_stored_key_to_a_new_host(client: TestClient, headers: dict[str, str], db: Database):
    _indexer(db, api_key="real-secret")
    with patch("trackseerr.api.routes.indexers.get_indexer_driver") as gid:
        resp = client.post("/api/settings/indexers/test", headers=headers,
                           json={"id": "ix1", "host_url": "http://192.168.1.99:9696/api", "api_key": mask_secret("real-secret")})
        assert resp.status_code == 400
        gid.assert_not_called()
        gid.return_value.test_connection.return_value = (True, "ok")
        resp = client.post("/api/settings/indexers/test", headers=headers,
                           json={"id": "ix1", "host_url": "http://192.168.1.99:9696/api", "api_key": ""})
        assert resp.status_code == 200
        assert gid.call_args.args[0]["api_key"] == ""


def test_indexer_test_route_edited_mask_is_400(client: TestClient, headers: dict[str, str], db: Database):
    _indexer(db, api_key="real-secret")
    with patch("trackseerr.api.routes.indexers.get_indexer_driver") as gid:
        resp = client.post("/api/settings/indexers/test", headers=headers,
                           json={"id": "ix1", "host_url": "http://192.168.1.5:9696/api", "api_key": "abcd•••••wxyz"})
    assert resp.status_code == 400
    gid.assert_not_called()


def test_indexer_test_route_unknown_id_with_masked_key_is_400(client: TestClient, headers: dict[str, str]):
    with patch("trackseerr.api.routes.indexers.get_indexer_driver") as gid:
        resp = client.post("/api/settings/indexers/test", headers=headers,
                           json={"id": "nope", "host_url": "http://192.168.1.5:9696/api", "api_key": "ab•••yz"})
    assert resp.status_code == 400
    gid.assert_not_called()


def test_indexer_test_route_plain_key_passes_through(client: TestClient, headers: dict[str, str]):
    driver = MagicMock()
    driver.test_connection.return_value = (True, "ok")
    with patch("trackseerr.api.routes.indexers.get_indexer_driver", return_value=driver) as gid:
        client.post("/api/settings/indexers/test", headers=headers,
                    json={"host_url": "http://192.168.1.5:9696/api", "api_key": "typed"})
    assert gid.call_args.args[0]["api_key"] == "typed"
