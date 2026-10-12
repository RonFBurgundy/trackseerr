"""v75 migration: Lidarr-local /MediaCover/ art paths are cleared so artist refresh refills them."""

from pathlib import Path

import pytest

from trackseerr.models import LibraryArtist
from trackseerr.storage import SCHEMA_VERSION, Database


@pytest.fixture
def test_db(tmp_path: Path):
    """Isolated disk-backed database."""
    db = Database(str(tmp_path / "v75.db"))
    yield db
    db.close()


def test_schema_version_is_at_least_75() -> None:
    assert SCHEMA_VERSION >= 75


def test_v75_clears_only_mediacover_paths(test_db: Database) -> None:
    conn = test_db.conn
    for aid, name, img, banner in (
        ("a1", "A1", "/MediaCover/1/poster.jpg?lastWrite=1", "/MediaCover/1/banner.jpg"),
        ("a2", "A2", "https://assets.fanart.tv/x.jpg", "/api/library/artists/a2/banner?v=1"),
    ):
        test_db.upsert_library_artist(
            LibraryArtist(id=aid, name=name, image_url=img, banner_url=banner)
        )
    for bid, aid, title, cover in (
        ("b1", "a1", "B1", "/MediaCover/Albums/2/cover.jpg"),
        ("b2", "a2", "B2", "/api/library/albums/x/cover?v=1"),
    ):
        test_db.upsert_library_album({"id": bid, "artist_id": aid, "title": title, "cover_url": cover})
        conn.execute("UPDATE library_albums SET art_version = 'v1' WHERE id = ?", (bid,))
    conn.commit()

    conn.execute("UPDATE library_artists SET art_version = 'av1' WHERE id = 'a1'")
    conn.commit()

    cur = conn.cursor()
    test_db._migration_v75(cur)
    conn.commit()

    rows = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, image_url, banner_url FROM library_artists")}
    assert rows["a1"] == (None, None)
    assert rows["a2"] == ("https://assets.fanart.tv/x.jpg", "/api/library/artists/a2/banner?v=1")
    albums = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, cover_url, art_version FROM library_albums")}
    assert conn.execute("SELECT art_version FROM library_artists WHERE id = 'a1'").fetchone()[0] == "av1"
    assert albums["b1"] == (None, "v1")
    assert albums["b2"] == ("/api/library/albums/x/cover?v=1", "v1")
