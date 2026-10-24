"""HTTP routes for slskd sharer chat challenges."""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.models import DownloadClientConfig, DownloadDriverType
from trackseerr.storage import Database


class FakeDriver:
    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.sent: list[tuple[str, str]] = []
        self.acked: list[str] = []

    def send_private_message(self, username: str, text: str) -> bool:
        self.sent.append((username, text))
        return self.accept

    def acknowledge_conversation(self, username: str) -> bool:
        self.acked.append(username)
        return True


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "chal.db"))
    d.update_media_management_settings({"root_folder_path": str(tmp_path), "library_mode": "native"})
    d.create_download_client(
        DownloadClientConfig(id="c1", name="slskd", driver_type=DownloadDriverType.SLSKD,
                             host_url="http://192.168.1.9:5030", enabled=True)
    )
    return d


@pytest.fixture
def config(tmp_path: Path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


def _headers(db: Database, config: Config, *, admin: bool = True) -> dict[str, str]:
    user = db.upsert_user("a-1" if admin else "u-1", "admin_user" if admin else "user", "u@example.com", is_admin=admin)
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=user["id"], username="x", is_admin=admin, secret_key=secret)
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(db, config):
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


def _seed(db: Database, mid: str = "m1") -> dict[str, Any]:
    row = db.record_slskd_challenge("c1", "sharer", mid, "human check: reply only with the word", "2026-10-01T00:00:00+00:00")
    assert row is not None
    return row


def test_list_open_challenges(client, db, config):
    a = _seed(db, "m1")
    b = _seed(db, "m2")
    db.set_slskd_challenge_status(b["id"], "dismissed")
    r = client.get("/api/slskd-challenges", headers=_headers(db, config))
    assert r.status_code == 200
    assert [i["id"] for i in r.json()["items"]] == [a["id"]]
    assert "reply_text" not in r.json()["items"][0]


def test_list_all(client, db, config):
    _seed(db, "m1")
    b = _seed(db, "m2")
    db.set_slskd_challenge_status(b["id"], "dismissed")
    r = client.get("/api/slskd-challenges?status=all", headers=_headers(db, config))
    assert len(r.json()["items"]) == 2
    r = client.get("/api/slskd-challenges?status=dismissed", headers=_headers(db, config))
    assert [i["id"] for i in r.json()["items"]] == [b["id"]]


def test_reply_sends_exact_text_and_marks_replied(client, db, config):
    row = _seed(db)
    driver = FakeDriver()
    with patch("trackseerr.slskd_challenges.get_acquisition_driver", return_value=driver):
        r = client.post(f"/api/slskd-challenges/{row['id']}/reply", json={"text": "  banana  "},
                        headers=_headers(db, config))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "replied"
    assert driver.sent == [("sharer", "banana")]
    assert driver.acked == ["sharer"]


def test_reply_empty_text_422(client, db, config):
    row = _seed(db)
    r = client.post(f"/api/slskd-challenges/{row['id']}/reply", json={"text": ""}, headers=_headers(db, config))
    assert r.status_code == 422


def test_reply_unknown_404(client, db, config):
    r = client.post("/api/slskd-challenges/nope/reply", json={"text": "x"}, headers=_headers(db, config))
    assert r.status_code == 404


def test_reply_closed_400(client, db, config):
    row = _seed(db)
    db.set_slskd_challenge_status(row["id"], "dismissed")
    driver = FakeDriver()
    with patch("trackseerr.slskd_challenges.get_acquisition_driver", return_value=driver):
        r = client.post(f"/api/slskd-challenges/{row['id']}/reply", json={"text": "x"}, headers=_headers(db, config))
    assert r.status_code == 400
    assert driver.sent == []


def test_reply_slskd_refused_502(client, db, config):
    row = _seed(db)
    with patch("trackseerr.slskd_challenges.get_acquisition_driver", return_value=FakeDriver(accept=False)):
        r = client.post(f"/api/slskd-challenges/{row['id']}/reply", json={"text": "x"}, headers=_headers(db, config))
    assert r.status_code == 502
    assert "slskd" in r.json()["detail"]
    assert db.get_slskd_challenge(row["id"])["status"] == "open"


def test_dismiss(client, db, config):
    row = _seed(db)
    r = client.post(f"/api/slskd-challenges/{row['id']}/dismiss", headers=_headers(db, config))
    assert r.status_code == 200 and r.json()["status"] == "dismissed"
    r = client.post("/api/slskd-challenges/nope/dismiss", headers=_headers(db, config))
    assert r.status_code == 404


def test_non_admin_forbidden(client, db, config):
    row = _seed(db)
    h = _headers(db, config, admin=False)
    assert client.get("/api/slskd-challenges", headers=h).status_code == 403
    assert client.post(f"/api/slskd-challenges/{row['id']}/reply", json={"text": "x"}, headers=h).status_code == 403
    assert client.post(f"/api/slskd-challenges/{row['id']}/dismiss", headers=h).status_code == 403
