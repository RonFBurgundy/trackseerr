"""v77 migration and store: library_track_artists."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def _credit(name: str, source: str, role: str = "primary") -> dict:
    return {"artist_name": name, "artist_mbid": None, "role": role, "join_phrase": "", "source": source}


def _db_with_track(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "v77.db"))
    db.upsert_library_artist({"id": "va", "name": "Various Artists"})
    db.upsert_library_album({"id": "al", "artist_id": "va", "title": "Compilation"})
    db.upsert_library_track({"id": "t1", "album_id": "al", "artist_id": "va", "title": "Stayin' Alive", "track_number": 1})
    db.upsert_library_track({"id": "t2", "album_id": "al", "artist_id": "va", "title": "Other", "track_number": 2})
    return db


def test_schema_version_is_at_least_77() -> None:
    assert SCHEMA_VERSION >= 77


def test_v77_creates_table_idempotently(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "m.db"))
    try:
        db.conn.execute("DROP TABLE library_track_artists")
        db.conn.commit()
        cur = db.conn.cursor()
        db._migration_v77(cur)
        db._migration_v77(cur)
        db.conn.commit()
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(library_track_artists)")}
        assert {"track_id", "position", "artist_name", "artist_mbid", "role", "join_phrase", "source"} <= cols
        idx = {r[1] for r in db.conn.execute("PRAGMA index_list(library_track_artists)")}
        assert {"idx_lib_track_artists_mbid", "idx_lib_track_artists_name"} <= idx
    finally:
        db.close()


def test_replace_and_list_round_trip(tmp_path: Path) -> None:
    db = _db_with_track(tmp_path)
    try:
        db.replace_track_artists("t1", [_credit("Bee Gees", "musicbrainz"), _credit("Guest", "musicbrainz", "featured")])
        got = db.list_track_artists("t1")
        assert [(c["position"], c["artist_name"], c["role"]) for c in got] == [(0, "Bee Gees", "primary"), (1, "Guest", "featured")]
        db.replace_track_artists("t1", [_credit("Solo", "musicbrainz")])
        assert [c["artist_name"] for c in db.list_track_artists("t1")] == ["Solo"]
        db.replace_track_artists("t2", [_credit("Other Act", "tags")])
        by_album = db.list_track_artists_for_album("al")
        assert set(by_album) == {"t1", "t2"}
        assert by_album["t2"][0]["artist_name"] == "Other Act"
        assert db.list_track_artists_for_album("nope") == {}
    finally:
        db.close()


def test_credits_cascade_with_track(tmp_path: Path) -> None:
    db = _db_with_track(tmp_path)
    try:
        db.replace_track_artists("t1", [_credit("Bee Gees", "musicbrainz")])
        db.conn.execute("DELETE FROM library_tracks WHERE id = 't1'")
        db.conn.commit()
        assert db.list_track_artists("t1") == []
    finally:
        db.close()


def test_precedence_musicbrainz_not_overwritten(tmp_path: Path) -> None:
    db = _db_with_track(tmp_path)
    try:
        db.replace_track_artists("t1", [_credit("MB Name", "musicbrainz")])
        db.replace_track_artists("t1", [_credit("Tag Name", "tags")])
        db.replace_track_artists("t1", [_credit("Title Name", "title", "remixer")])
        assert [c["artist_name"] for c in db.list_track_artists("t1")] == ["MB Name"]
    finally:
        db.close()


def test_precedence_musicbrainz_replaces_tags_and_tags_replace_title(tmp_path: Path) -> None:
    db = _db_with_track(tmp_path)
    try:
        db.replace_track_artists("t1", [_credit("T", "title", "remixer")])
        db.replace_track_artists("t1", [_credit("Tag Name", "tags")])
        assert [c["artist_name"] for c in db.list_track_artists("t1")] == ["Tag Name"]
        db.replace_track_artists("t1", [_credit("MB Name", "musicbrainz")])
        assert [c["artist_name"] for c in db.list_track_artists("t1")] == ["MB Name"]
    finally:
        db.close()
