"""SlskdChallengeStoreMixin: challenge rows and gated-peer state."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from trackseerr.storage import Database


@pytest.fixture
def test_db(tmp_path: Path) -> Iterator[Database]:
    db = Database(str(tmp_path / "chal.db"))
    yield db
    db.close()


def _rec(db: Database, mid: str, received: str = "2026-01-01T00:00:00+00:00", **kw):
    return db.record_slskd_challenge("c1", "Bob", mid, "Human check", received, **kw)


def test_record_is_idempotent_per_message(test_db: Database) -> None:
    first = _rec(test_db, "m1", download_title="T")
    assert first is not None and first["id"].startswith("chal-") and first["status"] == "open"
    assert first["download_title"] == "T"
    assert _rec(test_db, "m1") is None
    assert _rec(test_db, "m2") is not None


def test_list_filters_by_status_newest_first(test_db: Database) -> None:
    a = _rec(test_db, "m1", "2026-01-01T00:00:00+00:00")
    b = _rec(test_db, "m2", "2026-01-02T00:00:00+00:00")
    c = _rec(test_db, "m3", "2026-01-03T00:00:00+00:00")
    test_db.set_slskd_challenge_status(a["id"], "dismissed")
    assert [r["id"] for r in test_db.list_slskd_challenges()] == [c["id"], b["id"]]
    assert [r["id"] for r in test_db.list_slskd_challenges(status=None)] == [c["id"], b["id"], a["id"]]
    assert [r["id"] for r in test_db.list_slskd_challenges(status="dismissed")] == [a["id"]]
    assert len(test_db.list_slskd_challenges(status=None, limit=2)) == 2
    assert test_db.get_slskd_challenge(b["id"])["message_id"] == "m2"
    assert test_db.get_slskd_challenge("nope") is None


def test_set_status_replied_stamps_time(test_db: Database) -> None:
    row = _rec(test_db, "m1")
    assert row["replied_at"] is None
    out = test_db.set_slskd_challenge_status(row["id"], "replied", reply_text="ok")
    assert out["status"] == "replied" and out["replied_at"] and out["reply_text"] == "ok"
    dismissed = test_db.set_slskd_challenge_status(_rec(test_db, "m2")["id"], "dismissed")
    assert dismissed["replied_at"] is None


def test_set_status_rejects_unknown(test_db: Database) -> None:
    row = _rec(test_db, "m1")
    with pytest.raises(ValueError):
        test_db.set_slskd_challenge_status(row["id"], "bogus")
    assert test_db.get_slskd_challenge(row["id"])["status"] == "open"


def test_expire_only_past_open_rows(test_db: Database) -> None:
    past = _rec(test_db, "m1", expires_at="2026-01-01T00:00:00+00:00")
    future = _rec(test_db, "m2", expires_at="2027-01-01T00:00:00+00:00")
    unknown = _rec(test_db, "m3")
    done = _rec(test_db, "m4", expires_at="2026-01-01T00:00:00+00:00")
    test_db.set_slskd_challenge_status(done["id"], "replied")
    assert test_db.expire_slskd_challenges("2026-06-01T00:00:00+00:00") == 1
    assert test_db.get_slskd_challenge(past["id"])["status"] == "expired"
    assert test_db.get_slskd_challenge(future["id"])["status"] == "open"
    assert test_db.get_slskd_challenge(unknown["id"])["status"] == "open"
    assert test_db.get_slskd_challenge(done["id"])["status"] == "replied"


def test_excluded_peers_only_active_gated(test_db: Database) -> None:
    now = "2026-06-01T00:00:00+00:00"
    test_db.set_slskd_peer_state("c1", "gated1", "gated", "2026-07-01T00:00:00+00:00")
    test_db.set_slskd_peer_state("c1", "unlocked1", "unlocked", "2026-07-01T00:00:00+00:00")
    test_db.set_slskd_peer_state("c1", "lapsed1", "gated", "2026-05-01T00:00:00+00:00")
    test_db.set_slskd_peer_state("c2", "other", "gated", "2026-07-01T00:00:00+00:00")
    assert test_db.excluded_slskd_peers("c1", now) == {"gated1"}
    test_db.set_slskd_peer_state("c1", "gated1", "unlocked", "2026-07-01T00:00:00+00:00")
    assert test_db.excluded_slskd_peers("c1", now) == set()


def test_peer_state_lapses_after_until(test_db: Database) -> None:
    test_db.set_slskd_peer_state("c1", "bob", "gated", "2026-06-01T00:00:00+00:00")
    assert test_db.slskd_peer_state("c1", "bob", "2026-05-31T00:00:00+00:00") == "gated"
    assert test_db.slskd_peer_state("c1", "bob", "2026-06-02T00:00:00+00:00") is None
    assert test_db.slskd_peer_state("c1", "nobody", "2026-05-31T00:00:00+00:00") is None


def test_peer_names_case_insensitive(test_db: Database) -> None:
    until = "2026-07-01T00:00:00+00:00"
    test_db.set_slskd_peer_state("c1", "  BoB ", "gated", until)
    test_db.set_slskd_peer_state("c1", "bob", "gated", until)
    assert test_db.excluded_slskd_peers("c1", "2026-06-01T00:00:00+00:00") == {"bob"}
    assert test_db.slskd_peer_state("c1", "BOB", "2026-06-01T00:00:00+00:00") == "gated"
