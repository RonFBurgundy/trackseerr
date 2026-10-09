"""Hardening tests for the user-facing issue-report endpoint."""

import threading
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.models import MusicRequest, NotificationEvent, RequestStatus
from tests.test_governance_and_issues import (  # noqa: F401  (fixtures)
    _auth_headers,
    seeded_users,
    test_config,
    test_db,
)


@pytest.fixture
def client(test_db, test_config, seeded_users):
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    with patch("trackseerr.api.routes.issues.notification_dispatcher"):
        yield TestClient(app)


def _body(**over):
    body = {"media_title": "Abbey Road", "artist": "The Beatles", "issue_type": "audio_quality", "problem_details": "crackle"}
    body.update(over)
    return body


def _hdr(name, seeded_users, test_db, test_config):
    return _auth_headers(seeded_users[name], test_db, test_config)


class TestValidation:
    @pytest.mark.parametrize(
        "over",
        [
            {"media_title": ""},
            {"media_title": "   "},
            {"media_title": "x" * 301},
            {"artist": ""},
            {"artist": " \t "},
            {"artist": "x" * 301},
            {"problem_details": ""},
            {"problem_details": "  \n "},
            {"problem_details": "x" * 2001},
            {"problem_details": "bad\x00byte"},
            {"problem_details": "bell\x07"},
            {"problem_details": "cr\rhere"},
            {"issue_type": "bogus"},
            {"issue_type": ""},
        ],
    )
    def test_rejected_422(self, client, test_db, test_config, seeded_users, over):
        r = client.post("/api/issues", json=_body(**over), headers=_hdr("alice", seeded_users, test_db, test_config))
        assert r.status_code == 422, r.text
        assert test_db.list_issues() == []

    def test_boundaries_and_stripping(self, client, test_db, test_config, seeded_users):
        h = _hdr("alice", seeded_users, test_db, test_config)
        r = client.post(
            "/api/issues",
            json=_body(media_title="  " + "t" * 300 + " ", artist="a", problem_details="line1\n\tline2 "),
            headers=h,
        )
        assert r.status_code == 201, r.text
        assert r.json()["media_title"] == "t" * 300
        assert r.json()["problem_details"] == "line1\n\tline2"
        r = client.post("/api/issues", json=_body(media_title="Other", problem_details="p" * 2000), headers=h)
        assert r.status_code == 201


class TestRequestOwnership:
    def _make_request(self, test_db, owner):
        test_db.create_request(
            MusicRequest(id="req-1", user_id=owner["id"], item_type="album", title="T", artist="A", status=RequestStatus.PENDING)
        )

    def test_foreign_request_404_creates_nothing(self, client, test_db, test_config, seeded_users):
        self._make_request(test_db, seeded_users["bob"])
        r = client.post("/api/issues", json=_body(request_id="req-1"), headers=_hdr("alice", seeded_users, test_db, test_config))
        assert r.status_code == 404
        assert test_db.list_issues() == []

    def test_unknown_request_404(self, client, test_db, test_config, seeded_users):
        r = client.post("/api/issues", json=_body(request_id="nope"), headers=_hdr("alice", seeded_users, test_db, test_config))
        assert r.status_code == 404

    def test_own_request_ok_and_admin_any(self, client, test_db, test_config, seeded_users):
        self._make_request(test_db, seeded_users["bob"])
        assert client.post("/api/issues", json=_body(request_id="req-1"), headers=_hdr("bob", seeded_users, test_db, test_config)).status_code == 201
        r = client.post("/api/issues", json=_body(request_id="req-1", media_title="Z"), headers=_hdr("admin", seeded_users, test_db, test_config))
        assert r.status_code == 201


class TestRateLimit:
    def _post_n(self, client, h, n, start=0):
        return [client.post("/api/issues", json=_body(media_title=f"T{i}"), headers=h).status_code for i in range(start, start + n)]

    def test_eleventh_is_429(self, client, test_db, test_config, seeded_users):
        h = _hdr("alice", seeded_users, test_db, test_config)
        assert self._post_n(client, h, 10) == [201] * 10
        r = client.post("/api/issues", json=_body(media_title="T10"), headers=h)
        assert r.status_code == 429
        assert "10 issues per 24 hours" in r.json()["detail"]
        assert len(test_db.list_issues(user_id="user-alice")) == 10
        # other users unaffected
        assert client.post("/api/issues", json=_body(), headers=_hdr("bob", seeded_users, test_db, test_config)).status_code == 201

    def test_old_issues_fall_out_of_window(self, client, test_db, test_config, seeded_users):
        h = _hdr("alice", seeded_users, test_db, test_config)
        self._post_n(client, h, 10)
        test_db.conn.execute(
            "UPDATE media_issues SET created_at = datetime('now', '-25 hours') WHERE id IN "
            "(SELECT id FROM media_issues LIMIT 1)"
        )
        test_db.conn.commit()
        assert client.post("/api/issues", json=_body(media_title="fresh"), headers=h).status_code == 201

    def test_admin_exempt(self, client, test_db, test_config, seeded_users):
        h = _hdr("admin", seeded_users, test_db, test_config)
        assert self._post_n(client, h, 12) == [201] * 12

    def test_concurrent_posts_never_exceed_cap(self, client, test_db, test_config, seeded_users):
        h = _hdr("alice", seeded_users, test_db, test_config)
        results: list[int] = []
        guard = threading.Lock()
        barrier = threading.Barrier(20)

        def worker(i: int) -> None:
            barrier.wait()
            code = client.post("/api/issues", json=_body(media_title=f"C{i}"), headers=h).status_code
            with guard:
                results.append(code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(201) == 10
        assert results.count(429) == 10
        assert len(test_db.list_issues(user_id="user-alice")) == 10


class TestDuplicates:
    def test_duplicate_open_409_with_id(self, client, test_db, test_config, seeded_users):
        h = _hdr("alice", seeded_users, test_db, test_config)
        first = client.post("/api/issues", json=_body(), headers=h).json()
        r = client.post("/api/issues", json=_body(media_title="ABBEY road", artist="the beatles"), headers=h)
        assert r.status_code == 409
        assert first["id"] in r.json()["detail"]
        assert len(test_db.list_issues()) == 1

    def test_in_progress_still_duplicate(self, client, test_db, test_config, seeded_users):
        h = _hdr("alice", seeded_users, test_db, test_config)
        first = client.post("/api/issues", json=_body(), headers=h).json()
        test_db.update_issue(first["id"], {"status": "in_progress"})
        assert client.post("/api/issues", json=_body(), headers=h).status_code == 409

    def test_resolved_or_other_type_or_other_user_allowed(self, client, test_db, test_config, seeded_users):
        h = _hdr("alice", seeded_users, test_db, test_config)
        first = client.post("/api/issues", json=_body(), headers=h).json()
        assert client.post("/api/issues", json=_body(issue_type="other"), headers=h).status_code == 201
        assert client.post("/api/issues", json=_body(), headers=_hdr("bob", seeded_users, test_db, test_config)).status_code == 201
        test_db.update_issue(first["id"], {"status": "resolved"})
        assert client.post("/api/issues", json=_body(), headers=h).status_code == 201


class TestNotification:
    def test_problem_details_truncated(self, test_db, test_config, seeded_users):
        app = create_app(db=test_db, config=test_config)
        app.dependency_overrides[get_db] = lambda: test_db
        app.dependency_overrides[get_config] = lambda: test_config
        c = TestClient(app)
        with patch("trackseerr.api.routes.issues.notification_dispatcher") as disp:
            r = c.post("/api/issues", json=_body(problem_details="d" * 2000), headers=_hdr("alice", seeded_users, test_db, test_config))
        assert r.status_code == 201
        assert len(r.json()["problem_details"]) == 2000  # stored in full
        event, data, _db = disp.dispatch.call_args.args
        assert event == NotificationEvent.ISSUE_REPORTED
        assert len(data["problem_details"]) == 500


class TestListFilters:
    def test_filters_scoped_to_caller(self, client, test_db, test_config, seeded_users):
        a = _hdr("alice", seeded_users, test_db, test_config)
        b = _hdr("bob", seeded_users, test_db, test_config)
        ad = _hdr("admin", seeded_users, test_db, test_config)
        client.post("/api/issues", json=_body(), headers=a)
        client.post("/api/issues", json=_body(media_title="Other"), headers=a)
        client.post("/api/issues", json=_body(), headers=b)

        r = client.get("/api/issues", params={"media_title": "abbey ROAD", "artist": "THE BEATLES"}, headers=a)
        assert [i["user_id"] for i in r.json()] == ["user-alice"]
        assert client.get("/api/issues", params={"media_title": "Other"}, headers=b).json() == []
        assert client.get("/api/issues", params={"media_title": "Abbey"}, headers=a).json() == []  # exact
        assert len(client.get("/api/issues", params={"media_title": "Abbey Road"}, headers=ad).json()) == 2
