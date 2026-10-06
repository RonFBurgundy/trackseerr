"""Issue lifecycle: validated statuses, request_stuck, dedupe, comments, unread, fix actions, tier placement."""

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync import internal_auth
from plex_playlist_sync.api import tier_middleware as tm
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.api.routes import issues as issues_mod
from plex_playlist_sync.clients.core_client import CoreClient, ProxyResponse
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import MusicRequest, NotificationEvent, RequestStatus
from plex_playlist_sync.storage import SCHEMA_VERSION, Database
from tests.test_governance_and_issues import (  # noqa: F401  (fixtures)
    _auth_headers,
    seeded_users,
    test_config,
    test_db,
)


@pytest.fixture
def dispatcher():
    with patch("plex_playlist_sync.api.routes.issues.notification_dispatcher") as disp:
        yield disp


@pytest.fixture
def client(test_db, test_config, seeded_users, dispatcher):
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    return TestClient(app)


@pytest.fixture
def hdr(test_db, test_config, seeded_users):
    return {name: _auth_headers(user, test_db, test_config) for name, user in seeded_users.items()}


def _body(**over: Any) -> dict[str, Any]:
    body = {"media_title": "Abbey Road", "artist": "The Beatles", "issue_type": "audio_quality", "problem_details": "crackle"}
    body.update(over)
    return body


def _create(client, headers, **over) -> dict[str, Any]:
    res = client.post("/api/issues", json=_body(**over), headers=headers)
    assert res.status_code == 201, res.text
    return res.json()


@pytest.fixture
def library(test_db):
    test_db.upsert_library_artist({"id": "ar-1", "name": "The Beatles"})
    test_db.upsert_library_album({"id": "alb-1", "artist_id": "ar-1", "title": "Abbey Road"})
    for n in (1, 2):
        test_db.upsert_library_track(
            {"id": f"trk-{n}", "album_id": "alb-1", "artist_id": "ar-1", "title": f"Song {n}", "track_number": n}
        )
    return "alb-1"


def _walk(value: Any):
    """Every key anywhere in a JSON value."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _walk(v)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


# ------------------------------------------------------------------------------------------ status lifecycle


class TestStatusValidation:
    @pytest.mark.parametrize("bad", ["closed", "done", "", "OPEN"])
    def test_admin_put_rejects_unknown_status_422(self, client, hdr, test_db, bad):
        issue = _create(client, hdr["alice"])
        res = client.put(f"/api/issues/{issue['id']}", json={"status": bad, "problem_details": "x"}, headers=hdr["admin"])
        assert res.status_code == 422
        assert test_db.get_issue(issue["id"])["status"] == "open"

    def test_put_cannot_smuggle_other_fields(self, client, hdr, test_db):
        issue = _create(client, hdr["alice"])
        res = client.put(
            f"/api/issues/{issue['id']}",
            json={"status": "in_progress", "media_title": "Hacked", "user_id": "x", "issue_type": "other"},
            headers=hdr["admin"],
        )
        assert res.status_code == 422
        row = test_db.get_issue(issue["id"])
        assert row["media_title"] == "Abbey Road" and row["status"] == "open"

    def test_put_noop_is_409_and_empty_body_422(self, client, hdr):
        issue = _create(client, hdr["alice"])
        assert client.put(f"/api/issues/{issue['id']}", json={"status": "open"}, headers=hdr["admin"]).status_code == 409
        assert client.put(f"/api/issues/{issue['id']}", json={}, headers=hdr["admin"]).status_code == 422

    def test_resolve_records_resolved_at_and_by_and_reopen_clears(self, client, hdr, test_db, seeded_users):
        issue = _create(client, hdr["alice"])
        res = client.put(f"/api/issues/{issue['id']}", json={"status": "resolved"}, headers=hdr["admin"])
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "resolved" and body["resolved_at"]
        assert body["resolved_by"] == seeded_users["admin"]["username"]
        reopened = client.put(f"/api/issues/{issue['id']}", json={"status": "open"}, headers=hdr["admin"]).json()
        assert reopened["resolved_at"] is None and reopened["resolved_by"] is None

    def test_admin_may_go_any_to_any(self, client, hdr):
        issue = _create(client, hdr["alice"])
        for target in ("wont_fix", "in_progress", "resolved", "open"):
            res = client.put(f"/api/issues/{issue['id']}", json={"status": target}, headers=hdr["admin"])
            assert res.status_code == 200 and res.json()["status"] == target

    def test_storage_rejects_invalid_status(self, test_db, seeded_users):
        row = test_db.create_issue(
            {"id": "i1", "user_id": seeded_users["alice"]["id"], "media_title": "a", "artist": "b",
             "issue_type": "other", "problem_details": "c"}
        )
        with pytest.raises(ValueError):
            test_db.update_issue(row["id"], {"status": "bogus"})
        with pytest.raises(ValueError):
            test_db.set_issue_status(row["id"], "closed", None, False)


class TestReporterTransitions:
    def test_reporter_can_close_then_reopen_own_issue(self, client, hdr):
        issue = _create(client, hdr["alice"])
        closed = client.post(f"/api/issues/{issue['id']}/status", json={"status": "resolved"}, headers=hdr["alice"])
        assert closed.status_code == 200 and closed.json()["status"] == "resolved"
        reopened = client.post(f"/api/issues/{issue['id']}/status", json={"status": "open"}, headers=hdr["alice"])
        assert reopened.status_code == 200 and reopened.json()["status"] == "open"

    def test_reporter_can_reopen_wont_fix(self, client, hdr, test_db):
        issue = _create(client, hdr["alice"])
        client.put(f"/api/issues/{issue['id']}", json={"status": "wont_fix"}, headers=hdr["admin"])
        res = client.post(f"/api/issues/{issue['id']}/status", json={"status": "open"}, headers=hdr["alice"])
        assert res.status_code == 200 and res.json()["status"] == "open"

    @pytest.mark.parametrize("target", ["in_progress", "wont_fix"])
    def test_reporter_cannot_set_other_statuses(self, client, hdr, test_db, target):
        issue = _create(client, hdr["alice"])
        res = client.post(f"/api/issues/{issue['id']}/status", json={"status": target}, headers=hdr["alice"])
        assert res.status_code == 403
        assert test_db.get_issue(issue["id"])["status"] == "open"

    def test_reporter_noop_409_and_invalid_422(self, client, hdr):
        issue = _create(client, hdr["alice"])
        assert client.post(f"/api/issues/{issue['id']}/status", json={"status": "open"}, headers=hdr["alice"]).status_code == 409
        assert client.post(f"/api/issues/{issue['id']}/status", json={"status": "closed"}, headers=hdr["alice"]).status_code == 422

    def test_other_user_gets_404_and_nothing_changes(self, client, hdr, test_db):
        issue = _create(client, hdr["alice"])
        res = client.post(f"/api/issues/{issue['id']}/status", json={"status": "resolved"}, headers=hdr["bob"])
        assert res.status_code == 404
        assert test_db.get_issue(issue["id"])["status"] == "open"

    def test_reporter_put_still_forbidden(self, client, hdr):
        issue = _create(client, hdr["alice"])
        assert client.put(f"/api/issues/{issue['id']}", json={"status": "resolved"}, headers=hdr["alice"]).status_code == 403

    def test_status_changes_fire_notification_events(self, client, hdr, dispatcher):
        issue = _create(client, hdr["alice"])
        dispatcher.dispatch.reset_mock()
        client.put(f"/api/issues/{issue['id']}", json={"status": "in_progress"}, headers=hdr["admin"])
        assert dispatcher.dispatch.call_args.args[0] == NotificationEvent.ISSUE_UPDATED
        client.put(f"/api/issues/{issue['id']}", json={"status": "resolved"}, headers=hdr["admin"])
        event, data, _ = dispatcher.dispatch.call_args.args
        assert event == NotificationEvent.ISSUE_RESOLVED
        assert data["status"] == "resolved" and "album_id" not in data


# ------------------------------------------------------------------------------------ request_stuck / refs


class TestRequestStuck:
    def _request(self, db, owner, status=RequestStatus.PROCESSING, rid="req-1"):
        db.create_request(MusicRequest(id=rid, user_id=owner["id"], item_type="album", title="T", artist="A", status=status))

    def test_happy_path(self, client, hdr, test_db, seeded_users):
        self._request(test_db, seeded_users["alice"])
        issue = _create(client, hdr["alice"], issue_type="request_stuck", request_id="req-1")
        assert issue["issue_type"] == "request_stuck" and issue["request_id"] == "req-1"

    def test_requires_request_id(self, client, hdr):
        res = client.post("/api/issues", json=_body(issue_type="request_stuck"), headers=hdr["alice"])
        assert res.status_code == 422

    def test_wrong_owner_404_admin_allowed(self, client, hdr, test_db, seeded_users):
        self._request(test_db, seeded_users["bob"])
        res = client.post("/api/issues", json=_body(issue_type="request_stuck", request_id="req-1"), headers=hdr["alice"])
        assert res.status_code == 404
        assert test_db.list_issues() == []
        res = client.post("/api/issues", json=_body(issue_type="request_stuck", request_id="req-1"), headers=hdr["admin"])
        assert res.status_code == 201

    @pytest.mark.parametrize("final", [RequestStatus.AVAILABLE, RequestStatus.REJECTED])
    def test_final_state_rejected_409(self, client, hdr, test_db, seeded_users, final):
        self._request(test_db, seeded_users["alice"], status=final)
        res = client.post("/api/issues", json=_body(issue_type="request_stuck", request_id="req-1"), headers=hdr["alice"])
        assert res.status_code == 409
        assert test_db.list_issues() == []

    @pytest.mark.parametrize("pending", [RequestStatus.PENDING, RequestStatus.APPROVED, RequestStatus.PROCESSING])
    def test_non_final_states_accepted(self, client, hdr, test_db, seeded_users, pending):
        self._request(test_db, seeded_users["alice"], status=pending)
        assert client.post("/api/issues", json=_body(issue_type="request_stuck", request_id="req-1"), headers=hdr["alice"]).status_code == 201


class TestMediaRefsAndDedupe:
    def test_requester_cannot_send_library_ids(self, client, hdr, library, test_db):
        for field in ("album_id", "track_id"):
            res = client.post("/api/issues", json=_body(**{field: "alb-1" if field == "album_id" else "trk-1"}), headers=hdr["alice"])
            assert res.status_code == 403
        assert test_db.list_issues() == []

    def test_admin_library_refs_stored_and_validated(self, client, hdr, library):
        issue = _create(client, hdr["admin"], album_id="alb-1", track_id="trk-1")
        assert issue["album_id"] == "alb-1" and issue["track_id"] == "trk-1"
        assert client.post("/api/issues", json=_body(media_title="N", album_id="nope"), headers=hdr["admin"]).status_code == 404
        assert client.post("/api/issues", json=_body(media_title="N", track_id="nope"), headers=hdr["admin"]).status_code == 404

    def test_requester_discovery_ref_roundtrip_without_library_ids(self, client, hdr):
        issue = _create(client, hdr["alice"], discovery_id="dz-123", item_type="album")
        assert issue["discovery_id"] == "dz-123" and issue["item_type"] == "album"
        assert "album_id" not in issue and "track_id" not in issue

    @pytest.mark.parametrize("over", [{"discovery_id": "x"}, {"item_type": "album"}, {"discovery_id": "x", "item_type": "playlist"}])
    def test_discovery_ref_needs_both_halves_and_valid_type(self, client, hdr, over):
        assert client.post("/api/issues", json=_body(**over), headers=hdr["alice"]).status_code == 422

    def test_dedupe_same_request_returns_409_with_existing_id(self, client, hdr, test_db, seeded_users):
        test_db.create_request(MusicRequest(id="req-1", user_id=seeded_users["alice"]["id"], item_type="album", title="T", artist="A"))
        first = _create(client, hdr["alice"], issue_type="request_stuck", request_id="req-1")
        res = client.post(
            "/api/issues", json=_body(issue_type="request_stuck", request_id="req-1", media_title="Another title"), headers=hdr["alice"]
        )
        assert res.status_code == 409
        assert res.json()["existing_issue_id"] == first["id"] and first["id"] in res.json()["detail"]

    def test_dedupe_same_discovery_ref(self, client, hdr):
        first = _create(client, hdr["alice"], discovery_id="dz-1", item_type="track")
        res = client.post("/api/issues", json=_body(media_title="Different", discovery_id="dz-1", item_type="track"), headers=hdr["alice"])
        assert res.status_code == 409 and res.json()["existing_issue_id"] == first["id"]

    def test_dedupe_same_library_ref_but_other_type_or_user_is_fine(self, client, hdr, library):
        _create(client, hdr["admin"], album_id="alb-1")
        res = client.post("/api/issues", json=_body(media_title="Z", album_id="alb-1"), headers=hdr["admin"])
        assert res.status_code == 409
        other_type = client.post("/api/issues", json=_body(media_title="Z", issue_type="corrupted_file", album_id="alb-1"), headers=hdr["admin"])
        assert other_type.status_code == 201

    def test_resolved_issue_does_not_block_a_new_one(self, client, hdr):
        first = _create(client, hdr["alice"], discovery_id="dz-9", item_type="track")
        client.put(f"/api/issues/{first['id']}", json={"status": "resolved"}, headers=hdr["admin"])
        assert client.post("/api/issues", json=_body(media_title="Q", discovery_id="dz-9", item_type="track"), headers=hdr["alice"]).status_code == 201


# ------------------------------------------------------------------------------------------------ comments


class TestComments:
    def test_post_and_list_by_reporter_and_admin(self, client, hdr):
        issue = _create(client, hdr["alice"])
        posted = client.post(f"/api/issues/{issue['id']}/comments", json={"body": "  still broken  "}, headers=hdr["alice"])
        assert posted.status_code == 201
        assert posted.json()["body"] == "still broken" and posted.json()["mine"] is True and posted.json()["is_admin"] is False
        reply = client.post(f"/api/issues/{issue['id']}/comments", json={"body": "looking"}, headers=hdr["admin"])
        assert reply.status_code == 201 and reply.json()["is_admin"] is True
        for who in ("alice", "admin"):
            listed = client.get(f"/api/issues/{issue['id']}/comments", headers=hdr[who]).json()
            assert [c["body"] for c in listed] == ["still broken", "looking"]

    def test_other_user_gets_404_on_read_and_write(self, client, hdr, test_db):
        issue = _create(client, hdr["alice"])
        assert client.get(f"/api/issues/{issue['id']}/comments", headers=hdr["bob"]).status_code == 404
        assert client.post(f"/api/issues/{issue['id']}/comments", json={"body": "hi"}, headers=hdr["bob"]).status_code == 404
        assert client.get("/api/issues/issue-nope/comments", headers=hdr["admin"]).status_code == 404
        assert test_db.list_issue_comments(issue["id"]) == []

    @pytest.mark.parametrize("body", ["", "   ", "x" * 2001, "bad\x00byte", "bell\x07"])
    def test_body_validation_422(self, client, hdr, body):
        issue = _create(client, hdr["alice"])
        assert client.post(f"/api/issues/{issue['id']}/comments", json={"body": body}, headers=hdr["alice"]).status_code == 422

    def test_body_boundary_and_newlines_allowed(self, client, hdr):
        issue = _create(client, hdr["alice"])
        assert client.post(f"/api/issues/{issue['id']}/comments", json={"body": "x" * 2000}, headers=hdr["alice"]).status_code == 201
        assert client.post(f"/api/issues/{issue['id']}/comments", json={"body": "a\n\tb"}, headers=hdr["alice"]).status_code == 201

    def test_admin_comment_moves_open_to_in_progress_reporter_comment_does_not(self, client, hdr, test_db):
        issue = _create(client, hdr["alice"])
        client.post(f"/api/issues/{issue['id']}/comments", json={"body": "hello"}, headers=hdr["alice"])
        assert test_db.get_issue(issue["id"])["status"] == "open"
        client.post(f"/api/issues/{issue['id']}/comments", json={"body": "on it"}, headers=hdr["admin"])
        assert test_db.get_issue(issue["id"])["status"] == "in_progress"

    def test_admin_comment_leaves_resolved_alone(self, client, hdr, test_db):
        issue = _create(client, hdr["alice"])
        client.put(f"/api/issues/{issue['id']}", json={"status": "resolved"}, headers=hdr["admin"])
        client.post(f"/api/issues/{issue['id']}/comments", json={"body": "fyi"}, headers=hdr["admin"])
        assert test_db.get_issue(issue["id"])["status"] == "resolved"

    def test_comment_count_and_last_activity(self, client, hdr):
        issue = _create(client, hdr["alice"])
        assert issue["comment_count"] == 0 and issue["last_activity_at"]
        client.post(f"/api/issues/{issue['id']}/comments", json={"body": "one"}, headers=hdr["alice"])
        client.post(f"/api/issues/{issue['id']}/comments", json={"body": "two"}, headers=hdr["admin"])
        got = client.get(f"/api/issues/{issue['id']}", headers=hdr["alice"]).json()
        assert got["comment_count"] == 2 and got["last_activity_at"] >= issue["last_activity_at"]
        listed = client.get("/api/issues", headers=hdr["admin"]).json()
        assert listed[0]["comment_count"] == 2

    def test_comment_cap_429(self, client, hdr, monkeypatch):
        monkeypatch.setattr(issues_mod, "ISSUE_COMMENT_LIMIT", 2)
        issue = _create(client, hdr["alice"])
        codes = [client.post(f"/api/issues/{issue['id']}/comments", json={"body": f"c{i}"}, headers=hdr["alice"]).status_code for i in range(3)]
        assert codes == [201, 201, 429]

    def test_comments_cascade_with_issue(self, client, hdr, test_db):
        issue = _create(client, hdr["alice"])
        client.post(f"/api/issues/{issue['id']}/comments", json={"body": "x"}, headers=hdr["alice"])
        client.delete(f"/api/issues/{issue['id']}", headers=hdr["admin"])
        assert test_db.conn.execute("SELECT COUNT(*) FROM issue_comments").fetchone()[0] == 0


# ------------------------------------------------------------------------------------------- unread / counts


class TestUnread:
    def test_admin_comment_makes_unread_and_seen_clears(self, client, hdr):
        issue = _create(client, hdr["alice"])
        assert issue["unread"] is False
        assert client.get("/api/issues/unread-count", headers=hdr["alice"]).json() == {"count": 0}
        client.post(f"/api/issues/{issue['id']}/comments", json={"body": "update"}, headers=hdr["admin"])
        assert client.get(f"/api/issues/{issue['id']}", headers=hdr["alice"]).json()["unread"] is True
        assert client.get("/api/issues/unread-count", headers=hdr["alice"]).json() == {"count": 1}
        seen = client.post(f"/api/issues/{issue['id']}/seen", headers=hdr["alice"])
        assert seen.status_code == 200 and seen.json()["unread"] is False
        assert client.get("/api/issues/unread-count", headers=hdr["alice"]).json() == {"count": 0}
        assert client.get(f"/api/issues/{issue['id']}", headers=hdr["alice"]).json()["unread"] is False

    def test_admin_status_change_is_unread_and_new_activity_after_seen_is_unread_again(self, client, hdr):
        issue = _create(client, hdr["alice"])
        client.put(f"/api/issues/{issue['id']}", json={"status": "resolved"}, headers=hdr["admin"])
        assert client.get("/api/issues/unread-count", headers=hdr["alice"]).json()["count"] == 1
        client.post(f"/api/issues/{issue['id']}/seen", headers=hdr["alice"])
        client.put(f"/api/issues/{issue['id']}", json={"status": "open"}, headers=hdr["admin"])
        assert client.get("/api/issues/unread-count", headers=hdr["alice"]).json()["count"] == 1

    def test_own_actions_are_never_unread(self, client, hdr):
        issue = _create(client, hdr["alice"])
        client.post(f"/api/issues/{issue['id']}/comments", json={"body": "mine"}, headers=hdr["alice"])
        client.post(f"/api/issues/{issue['id']}/status", json={"status": "resolved"}, headers=hdr["alice"])
        assert client.get("/api/issues/unread-count", headers=hdr["alice"]).json()["count"] == 0

    def test_unread_is_per_reporter_and_seen_on_foreign_issue_404(self, client, hdr):
        issue = _create(client, hdr["alice"])
        client.post(f"/api/issues/{issue['id']}/comments", json={"body": "u"}, headers=hdr["admin"])
        assert client.get("/api/issues/unread-count", headers=hdr["bob"]).json()["count"] == 0
        assert client.post(f"/api/issues/{issue['id']}/seen", headers=hdr["bob"]).status_code == 404

    def test_admin_seen_on_someone_elses_issue_does_not_clear_reporters_flag(self, client, hdr):
        issue = _create(client, hdr["alice"])
        client.post(f"/api/issues/{issue['id']}/comments", json={"body": "u"}, headers=hdr["admin"])
        assert client.post(f"/api/issues/{issue['id']}/seen", headers=hdr["admin"]).status_code == 200
        assert client.get("/api/issues/unread-count", headers=hdr["alice"]).json()["count"] == 1

    def test_open_count_admin_only(self, client, hdr):
        _create(client, hdr["alice"])
        second = _create(client, hdr["alice"], media_title="Other")
        client.put(f"/api/issues/{second['id']}", json={"status": "in_progress"}, headers=hdr["admin"])
        assert client.get("/api/issues/open-count", headers=hdr["admin"]).json() == {"count": 1, "in_progress": 1}
        assert client.get("/api/issues/open-count", headers=hdr["alice"]).status_code == 403
        assert client.get("/api/issues/open-count").status_code == 401


# --------------------------------------------------------------------------------------------- fix actions


class TestActions:
    def _action(self, client, headers, issue_id, action):
        return client.post(f"/api/issues/{issue_id}/actions/{action}", headers=headers)

    def _stuck(self, client, hdr, test_db, seeded_users):
        test_db.create_request(
            MusicRequest(id="req-1", user_id=seeded_users["alice"]["id"], item_type="album", title="T", artist="A", status=RequestStatus.PROCESSING)
        )
        return _create(client, hdr["alice"], issue_type="request_stuck", request_id="req-1")

    def test_available_actions_by_type_admin_only(self, client, hdr, library, test_db, seeded_users):
        stuck = self._stuck(client, hdr, test_db, seeded_users)
        quality = _create(client, hdr["admin"], media_title="Q", issue_type="corrupted_file", album_id="alb-1")
        wrong = _create(client, hdr["admin"], media_title="W", issue_type="wrong_release", track_id="trk-1")
        other = _create(client, hdr["admin"], media_title="O", issue_type="incorrect_tags")
        by_id = {i["id"]: i for i in client.get("/api/issues", headers=hdr["admin"]).json()}
        assert by_id[stuck["id"]]["available_actions"] == ["retry_request"]
        assert by_id[quality["id"]]["available_actions"] == ["research", "rematch"]
        assert by_id[wrong["id"]]["available_actions"] == ["blocklist_and_research", "rematch"]
        assert by_id[other["id"]]["available_actions"] == []
        assert "available_actions" not in client.get(f"/api/issues/{stuck['id']}", headers=hdr["alice"]).json()

    def test_retry_request_calls_requests_retry_and_comments(self, client, hdr, test_db, seeded_users):
        issue = self._stuck(client, hdr, test_db, seeded_users)
        with patch.object(issues_mod.requests_routes, "retry_request", return_value={"success": True, "status": "processing"}) as retry:
            res = self._action(client, hdr["admin"], issue["id"], "retry_request")
        assert res.status_code == 200, res.text
        assert retry.call_args.args == ("req-1",)
        body = res.json()
        assert body["action"] == "retry_request" and body["result"]["success"] is True
        assert body["issue"]["status"] == "in_progress"
        comments = test_db.list_issue_comments(issue["id"])
        assert comments[-1]["body"] == "Admin ran: Retry request" and comments[-1]["is_system"] == 1

    def test_research_calls_wanted_search_with_album_track_ids(self, client, hdr, library, test_db):
        issue = _create(client, hdr["admin"], issue_type="missing_tracks", album_id="alb-1")
        with patch.object(issues_mod.wanted_routes, "search_wanted", return_value={"queued": 2}) as search:
            res = self._action(client, hdr["admin"], issue["id"], "research")
        assert res.status_code == 200, res.text
        assert sorted(search.call_args.args[0].ids) == ["trk-1", "trk-2"]
        assert res.json()["issue"]["status"] == "in_progress"
        assert test_db.list_issue_comments(issue["id"])[-1]["body"] == "Admin ran: Search again"

    def test_research_nothing_queued_is_409_without_side_effects(self, client, hdr, library, test_db):
        issue = _create(client, hdr["admin"], issue_type="audio_quality", album_id="alb-1")
        with patch.object(issues_mod.wanted_routes, "search_wanted", return_value={"queued": 0, "message": "A search batch is already running"}):
            res = self._action(client, hdr["admin"], issue["id"], "research")
        assert res.status_code == 409 and "already running" in res.json()["detail"]
        assert test_db.get_issue(issue["id"])["status"] == "open"
        assert test_db.list_issue_comments(issue["id"]) == []

    def test_blocklist_and_research_blocklists_source_then_searches(self, client, hdr, library, test_db):
        test_db.record_download_event(
            "imported", album_id="alb-1", release_title="Bad.Release.FLAC", info_hash="ABCDEF", indexer="ix",
            protocol="torrent", artist="The Beatles", album="Abbey Road",
        )
        issue = _create(client, hdr["admin"], issue_type="wrong_release", album_id="alb-1")
        with patch.object(issues_mod.wanted_routes, "search_wanted", return_value={"queued": 2}) as search:
            res = self._action(client, hdr["admin"], issue["id"], "blocklist_and_research")
        assert res.status_code == 200, res.text
        assert res.json()["result"]["release"] == "Bad.Release.FLAC"
        assert search.called
        assert test_db.is_blocklisted(release_title="Bad.Release.FLAC")
        assert test_db.list_issue_comments(issue["id"])[-1]["body"] == "Admin ran: Blocklist release and search again"
        assert res.json()["issue"]["status"] == "in_progress"

    def test_blocklist_and_research_409_when_source_unknown(self, client, hdr, library, test_db):
        issue = _create(client, hdr["admin"], issue_type="wrong_release", album_id="alb-1")
        with patch.object(issues_mod.wanted_routes, "search_wanted") as search:
            res = self._action(client, hdr["admin"], issue["id"], "blocklist_and_research")
        assert res.status_code == 409 and "not on record" in res.json()["detail"]
        search.assert_not_called()
        assert test_db.conn.execute("SELECT COUNT(*) FROM download_blocklist").fetchone()[0] == 0
        assert test_db.get_issue(issue["id"])["status"] == "open"
        assert test_db.list_issue_comments(issue["id"]) == []

    def test_rematch_returns_scope_payload_with_no_side_effect(self, client, hdr, library, test_db):
        issue = _create(client, hdr["admin"], issue_type="incorrect_tags", album_id="alb-1")
        res = self._action(client, hdr["admin"], issue["id"], "rematch")
        assert res.status_code == 200, res.text
        result = res.json()["result"]
        assert result["scope"] == {"album_id": "alb-1"}
        assert result["album"]["title"] == "Abbey Road" and result["album"]["artist_name"] == "The Beatles"
        assert {t["id"] for t in result["tracks"]} == {"trk-1", "trk-2"}
        assert test_db.get_issue(issue["id"])["status"] == "open"
        assert test_db.list_issue_comments(issue["id"]) == []

    def test_unavailable_or_unknown_action(self, client, hdr, library):
        issue = _create(client, hdr["admin"], issue_type="incorrect_tags")
        assert self._action(client, hdr["admin"], issue["id"], "research").status_code == 409
        assert self._action(client, hdr["admin"], issue["id"], "nuke").status_code == 404
        assert self._action(client, hdr["admin"], "issue-nope", "research").status_code == 404

    def test_actions_are_admin_only(self, client, hdr, test_db, seeded_users):
        issue = self._stuck(client, hdr, test_db, seeded_users)
        with patch.object(issues_mod.requests_routes, "retry_request") as retry:
            assert self._action(client, hdr["alice"], issue["id"], "retry_request").status_code == 403
            assert self._action(client, hdr["manager"], issue["id"], "retry_request").status_code == 403
        retry.assert_not_called()

    def test_action_on_resolved_issue_reopens_to_in_progress(self, client, hdr, library, test_db):
        issue = _create(client, hdr["admin"], issue_type="corrupted_file", album_id="alb-1")
        client.put(f"/api/issues/{issue['id']}", json={"status": "resolved"}, headers=hdr["admin"])
        with patch.object(issues_mod.wanted_routes, "search_wanted", return_value={"queued": 1}):
            res = self._action(client, hdr["admin"], issue["id"], "research")
        assert res.json()["issue"]["status"] == "in_progress" and res.json()["issue"]["resolved_at"] is None

    def test_native_only_actions_unavailable_in_lidarr_mode(self, client, hdr, library):
        issue = _create(client, hdr["admin"], issue_type="corrupted_file", album_id="alb-1")
        with patch.object(issues_mod, "get_library_mode", return_value="lidarr"):
            got = client.get(f"/api/issues/{issue['id']}", headers=hdr["admin"]).json()
        assert got["available_actions"] == []


# ------------------------------------------------------------------------------------- requester whitelist


class TestRequesterWhitelist:
    FORBIDDEN = {"album_id", "track_id", "available_actions", "is_system", "file_path", "path", "release_title", "reporter_seen_at", "last_staff_activity_at", "resolved_by_id", "comment_count_all"}

    def test_no_admin_data_anywhere_in_requester_responses(self, client, hdr, library, test_db, seeded_users):
        test_db.create_request(
            MusicRequest(id="req-1", user_id=seeded_users["alice"]["id"], item_type="album", title="T", artist="A", status=RequestStatus.PROCESSING)
        )
        issue = _create(client, hdr["alice"], issue_type="request_stuck", request_id="req-1")
        # Admin attaches library refs on a *different* admin-created issue for the same album, then works alice's one.
        _create(client, hdr["admin"], media_title="Admin one", album_id="alb-1")
        client.post(f"/api/issues/{issue['id']}/comments", json={"body": "looking"}, headers=hdr["admin"])
        with patch.object(issues_mod.requests_routes, "retry_request", return_value={"success": True}):
            client.post(f"/api/issues/{issue['id']}/actions/retry_request", headers=hdr["admin"])
        # Even if library ids were stored on her issue, a requester never sees them.
        test_db.conn.execute("UPDATE media_issues SET album_id = 'alb-1', track_id = 'trk-1' WHERE id = ?", (issue["id"],))
        test_db.conn.commit()

        payloads = [
            client.get("/api/issues", headers=hdr["alice"]).json(),
            client.get(f"/api/issues/{issue['id']}", headers=hdr["alice"]).json(),
            client.get(f"/api/issues/{issue['id']}/comments", headers=hdr["alice"]).json(),
            client.post(f"/api/issues/{issue['id']}/comments", json={"body": "thanks"}, headers=hdr["alice"]).json(),
            client.post(f"/api/issues/{issue['id']}/status", json={"status": "resolved"}, headers=hdr["alice"]).json(),
            _create(client, hdr["alice"], media_title="Fresh"),
        ]
        for payload in payloads:
            assert not (set(_walk(payload)) & self.FORBIDDEN), payload
        assert "alb-1" not in json.dumps(payloads) and "Admin ran" not in json.dumps(payloads)

    def test_system_comments_hidden_from_reporter_but_visible_to_admin(self, client, hdr, test_db, seeded_users):
        issue = _create(client, hdr["alice"])
        test_db.add_issue_comment(issue["id"], seeded_users["admin"]["id"], "Admin ran: Search again", is_admin=True, is_system=True)
        assert client.get(f"/api/issues/{issue['id']}/comments", headers=hdr["alice"]).json() == []
        assert client.get(f"/api/issues/{issue['id']}", headers=hdr["alice"]).json()["comment_count"] == 0
        admin_view = client.get(f"/api/issues/{issue['id']}/comments", headers=hdr["admin"]).json()
        assert admin_view[0]["is_system"] is True
        assert client.get(f"/api/issues/{issue['id']}", headers=hdr["admin"]).json()["comment_count"] == 1

    def test_admin_response_carries_library_ids(self, client, hdr, library):
        issue = _create(client, hdr["admin"], album_id="alb-1")
        got = client.get(f"/api/issues/{issue['id']}", headers=hdr["admin"]).json()
        assert got["album_id"] == "alb-1" and "available_actions" in got


# ------------------------------------------------------------------------------------------------ two-tier

SECRET = "s" * 40


def _cfg(tmp_path: Path, role: str) -> Config:
    return Config(
        plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path), role=role,
        internal_core_secret=SECRET, trackseerr_core_url="http://core.internal:5251", user_request_quota=10,
    )


def _signed(method: str, target: str, user_id: str = "1001", user_name: str = "alice", body: bytes = b"") -> dict[str, str]:
    headers = internal_auth.sign_assertion(SECRET, method, target, user_id, user_name, body)
    if body:
        headers["Content-Type"] = "application/json"
    return headers


@pytest.fixture
def core(tmp_path):
    db = Database(":memory:")
    db.upsert_user("admin-1", "root", "a@x.tv", is_admin=True)
    db.upsert_user("1001", "alice", "al@x.tv", is_admin=False)
    cfg = _cfg(tmp_path, "core")
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    with patch("plex_playlist_sync.api.routes.issues.notification_dispatcher"):
        yield TestClient(app), db
    db.close()


class TestTierPlacement:
    def test_forwarded_user_can_use_user_scoped_routes(self, core):
        client, db = core
        body = json.dumps(_body()).encode()
        created = client.post("/api/issues", content=body, headers=_signed("POST", "/api/issues", body=body))
        assert created.status_code == 201 and created.json()["user_id"] == "1001"
        iid = created.json()["id"]
        cbody = json.dumps({"body": "hello"}).encode()
        path = f"/api/issues/{iid}/comments"
        assert client.post(path, content=cbody, headers=_signed("POST", path, body=cbody)).status_code == 201
        assert client.get(path, headers=_signed("GET", path)).status_code == 200
        assert client.get("/api/issues/unread-count", headers=_signed("GET", "/api/issues/unread-count")).json() == {"count": 0}
        seen = f"/api/issues/{iid}/seen"
        assert client.post(seen, headers=_signed("POST", seen)).status_code == 200

    def test_forwarded_admin_account_is_refused_everywhere(self, core):
        client, _ = core
        for method, path in (("GET", "/api/issues"), ("GET", "/api/issues/open-count"), ("POST", "/api/issues/x/actions/research")):
            res = client.request(method, path, headers=_signed(method, path, user_id="admin-1", user_name="root"))
            assert res.status_code == 403, path

    def test_forwarded_principal_is_never_admin_on_admin_routes(self, core):
        client, db = core
        body = json.dumps(_body()).encode()
        iid = client.post("/api/issues", content=body, headers=_signed("POST", "/api/issues", body=body)).json()["id"]
        path = f"/api/issues/{iid}"
        put_body = json.dumps({"status": "resolved"}).encode()
        assert client.put(path, content=put_body, headers=_signed("PUT", path, body=put_body)).status_code == 403
        assert client.delete(path, headers=_signed("DELETE", path)).status_code == 403
        assert client.get("/api/issues/open-count", headers=_signed("GET", "/api/issues/open-count")).status_code == 403
        act = f"{path}/actions/rematch"
        assert client.post(act, headers=_signed("POST", act)).status_code == 403
        assert db.get_issue(iid)["status"] == "open"

    def test_forwarded_cannot_send_library_ids(self, core):
        client, db = core
        db.upsert_library_artist({"id": "ar-1", "name": "A"})
        db.upsert_library_album({"id": "alb-1", "artist_id": "ar-1", "title": "T"})
        body = json.dumps(_body(album_id="alb-1")).encode()
        res = client.post("/api/issues", content=body, headers=_signed("POST", "/api/issues", body=body))
        assert res.status_code == 403

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/issues"),
            ("POST", "/api/issues"),
            ("GET", "/api/issues/issue-1"),
            ("GET", "/api/issues/unread-count"),
            ("GET", "/api/issues/issue-1/comments"),
            ("POST", "/api/issues/issue-1/comments"),
            ("POST", "/api/issues/issue-1/seen"),
            ("POST", "/api/issues/issue-1/status"),
        ],
    )
    def test_gateway_forwards_user_scoped_routes(self, tmp_path, method, path):
        db = Database(":memory:")
        db.upsert_user("1001", "alice", None, is_admin=False)
        cfg = _cfg(tmp_path, "gateway")
        app = create_app(db=db, config=cfg)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_config] = lambda: cfg
        from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key

        token = create_session_token(
            user_id="1001", username="alice", is_admin=False, secret_key=get_or_create_secret_key(data_dir=cfg.data_dir)
        )
        db.create_session(token, "1001", {"auth": "test"})
        fake = ProxyResponse(200, b"{}", {"Content-Type": "application/json"})
        with patch.object(CoreClient, "proxy", return_value=fake) as proxy:
            res = TestClient(app).request(method, path, headers={"Authorization": f"Bearer {token}"}, content=b"{}" if method == "POST" else None)
        db.close()
        assert res.status_code == 200
        assert proxy.call_args.args[:2] == (method, path)
        assert proxy.call_args.args[4]["id"] == "1001"

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/issues/open-count"),
            ("PUT", "/api/issues/issue-1"),
            ("DELETE", "/api/issues/issue-1"),
            ("POST", "/api/issues/issue-1/actions/retry_request"),
            ("POST", "/api/issues/issue-1/actions/rematch"),
        ],
    )
    def test_core_only_routes_are_not_on_any_gateway_allowlist(self, method, path):
        for table in (tm.GATEWAY_LOCAL_ALLOWLIST, tm.GATEWAY_FORWARD_SERVICE_ALLOWLIST):
            assert not tm._allowed(table, method, path)
        forwarded = tm._allowed(tm.GATEWAY_FORWARD_ALLOWLIST, method, path) and not tm._allowed(
            tm.GATEWAY_FORWARD_DENYLIST, method, path
        )
        assert not forwarded

    def test_gateway_404s_core_only_issue_routes_without_forwarding(self, tmp_path):
        db = Database(":memory:")
        db.upsert_user("1001", "alice", None, is_admin=False)
        cfg = _cfg(tmp_path, "gateway")
        app = create_app(db=db, config=cfg)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_config] = lambda: cfg
        from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key

        token = create_session_token(
            user_id="1001", username="alice", is_admin=False, secret_key=get_or_create_secret_key(data_dir=cfg.data_dir)
        )
        db.create_session(token, "1001", {"auth": "test"})
        client = TestClient(app)
        with patch.object(CoreClient, "proxy") as proxy:
            for method, path in (
                ("GET", "/api/issues/open-count"),
                ("POST", "/api/issues/issue-1/actions/research"),
                ("PUT", "/api/issues/issue-1"),
            ):
                assert client.request(method, path, headers={"Authorization": f"Bearer {token}"}).status_code == 404, path
        proxy.assert_not_called()
        db.close()


# ------------------------------------------------------------------------------------------------ migration


def test_migration_v61_upgrades_an_existing_database(tmp_path):
    assert SCHEMA_VERSION >= 61
    path = tmp_path / "old.db"
    db = Database(str(path))
    db.upsert_user("u1", "alice", None, is_admin=False)
    db.close()

    # Rebuild the pre-v61 shape: old columns only, no comments table, legacy status values.
    conn = sqlite3.connect(str(path))
    conn.execute("DROP TABLE issue_comments")
    conn.execute("DROP INDEX IF EXISTS idx_media_issues_album")
    for col in ("resolved_at", "resolved_by", "album_id", "track_id", "discovery_id", "item_type",
                "reporter_seen_at", "last_activity_at", "last_staff_activity_at"):
        conn.execute(f"ALTER TABLE media_issues DROP COLUMN {col}")
    for iid, st in (("i-open", "open"), ("i-res", "resolved"), ("i-closed", "closed"), ("i-junk", "banana"), ("i-prog", "in_progress")):
        conn.execute(
            "INSERT INTO media_issues (id, media_title, artist, issue_type, problem_details, status, user_id) "
            "VALUES (?, 'T', 'A', 'other', 'd', ?, 'u1')",
            (iid, st),
        )
    conn.execute("DELETE FROM schema_migrations WHERE version >= 61")
    conn.commit()
    conn.close()

    db = Database(str(path))
    try:
        assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
        statuses = {r["id"]: r["status"] for r in db.list_issues()}
        assert statuses == {
            "i-open": "open", "i-res": "resolved", "i-closed": "resolved", "i-junk": "open", "i-prog": "in_progress",
        }
        resolved = db.get_issue("i-res")
        assert resolved["resolved_at"] and resolved["last_activity_at"] and resolved["unread"] == 0
        assert db.get_issue("i-open")["resolved_at"] is None
        assert db.list_issue_comments("i-open") == []
        comment = db.add_issue_comment("i-open", "u1", "hi", is_admin=False)
        assert comment is not None and db.get_issue("i-open")["comment_count_all"] == 1
        with db._lock:
            db._migration_v61(db.conn.cursor())  # idempotent re-run
    finally:
        db.close()
