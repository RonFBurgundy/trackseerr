"""Tests for torrent pack index parsing, logic, matching, and database storage."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from trackseerr.pack_index import (
    looks_like_pack,
    parse_pack_file,
    search_text,
)
from trackseerr.storage import Database


def test_parse_pack_file_cases() -> None:
    # 1. Leading 3-digit track number with space and parenthesized mix
    artist, title = parse_pack_file("045 David Guetta - Titanium (Extended Mix).mp3")
    assert artist == "David Guetta"
    assert title == "Titanium (Extended Mix)"

    # 2. Leading track number with dot, en-dash, and trailing bracket tag [Clean]
    artist, title = parse_pack_file("01. Artist – Song [Clean].flac")
    assert artist == "Artist"
    assert title == "Song"

    # 3. Folder-level artist when file has no artist separator
    artist, title = parse_pack_file("Artist - Album/03 Song.flac")
    assert artist == "Artist"
    assert title == "Song"

    # 4. Bracketed track number, mix in parentheses, trailing bracket tag
    artist, title = parse_pack_file("[012] Artist - Song (Radio Edit) [www.pool.com].mp3")
    assert artist == "Artist"
    assert title == "Song (Radio Edit)"

    # 5. No-separator filename
    artist, title = parse_pack_file("01 JustATitle.flac")
    assert artist is None
    assert title == "JustATitle"

    # 6. Non-Latin artist and title
    artist, title = parse_pack_file("07 宇多田ヒカル - First Love.flac")
    assert artist == "宇多田ヒカル"
    assert title == "First Love"

    # 7. Disc-track format (1-05.)
    artist, title = parse_pack_file("1-05. Artist - Title.mp3")
    assert artist == "Artist"
    assert title == "Title"

    # 8. Leading dash after track number (045 - )
    artist, title = parse_pack_file("045 - Artist - Title.mp3")
    assert artist == "Artist"
    assert title == "Title"


def test_looks_like_pack() -> None:
    # Negative cases
    assert not looks_like_pack("David Guetta-Pop Life-CD-FLAC-2007-PERFECT")
    assert not looks_like_pack("David Guetta-Pop Life-CD-FLAC-2007-PERFECT", file_count=12)
    assert not looks_like_pack("Queen - A Night at the Opera (1975) [FLAC]", file_count=12)
    assert not looks_like_pack("Titanium", file_count=24)

    # Positive cases by keyword
    assert looks_like_pack("Various Artists - Summer Hits 2024")
    assert looks_like_pack("VA - Dance Hits 2024")
    assert looks_like_pack("Top 100 Billboard Singles")
    assert looks_like_pack("Top 40 Dance Chart 2024")
    assert looks_like_pack("Top 50 Hits of the 90s")
    assert looks_like_pack("DJ Pool May 2024")
    assert looks_like_pack("Dance Megapack 2024")
    assert looks_like_pack("Artist - Complete Discography")
    assert looks_like_pack("80s Compilation FLAC")
    assert looks_like_pack("Rock Collection 100")
    assert looks_like_pack("Acoustic Bundle 2024")
    assert looks_like_pack("Promo Only May 2024")
    assert looks_like_pack("Club Mixes Vol 12")
    assert looks_like_pack("V.A. - 90s Dance")

    # Positive case by file count
    assert looks_like_pack("Unknown Release Title", file_count=25)
    assert looks_like_pack("David Guetta-Pop Life-CD-FLAC-2007-PERFECT", file_count=200)


def test_search_text_non_latin() -> None:
    text = search_text("07 宇多田ヒカル - First Love.flac", "宇多田ヒカル", "First Love")
    assert "宇多田ヒカル" in text
    assert "first love" in text


def test_pack_store_lifecycle_and_search(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "pack_store.db"))
    try:
        # 1. Setup indexer
        idx = db.create_indexer(
            {
                "id": "idx-tl",
                "name": "TorrentLeech",
                "indexer_type": "torznab",
                "host_url": "https://tl.org",
                "pack_scan": "auto",
            }
        )
        assert idx["pack_scan"] == "auto"

        # 2. Upsert pack release is idempotent
        r1 = db.upsert_pack_release(
            indexer_id="idx-tl",
            guid="guid-100",
            title="VA - Top 400 Pool May 2024",
            download_url="https://tl.org/dl/100",
            seeders=42,
            size_bytes=1000000000,
        )
        assert r1["id"].startswith("pack-")
        assert r1["scan_status"] == "pending"

        r2 = db.upsert_pack_release(
            indexer_id="idx-tl",
            guid="guid-100",
            title="VA - Top 400 Pool May 2024",
            seeders=50,
        )
        assert r2["id"] == r1["id"]
        assert r2["seeders"] == 50

        # Pending list
        pending = db.list_pending_pack_releases(limit=10)
        assert len(pending) == 1
        assert pending[0]["id"] == r1["id"]

        # 3. Record files (simulate a 400-file pack)
        files = []
        files.append(
            {
                "file_index": 0,
                "path": "045 David Guetta - Titanium.mp3",
                "size_bytes": 8000000,
            }
        )
        files.append(
            {
                "file_index": 1,
                "path": "046 David Guetta feat. Sia - Titanium.flac",
                "size_bytes": 28000000,
            }
        )
        files.append(
            {
                "file_index": 2,
                "path": "047 David Guetta - Titanium (Live).mp3",
                "size_bytes": 8500000,
            }
        )
        files.append(
            {
                "file_index": 3,
                "path": "07 宇多田ヒカル - First Love.flac",
                "size_bytes": 25000000,
            }
        )
        files.append(
            {
                "file_index": 4,
                "path": "Cover.jpg",
                "size_bytes": 500000,
            }
        )
        for i in range(5, 400):
            files.append(
                {
                    "file_index": i,
                    "path": f"Track {i:03d} - Song {i}.mp3",
                    "size_bytes": 5000000,
                }
            )

        recorded = db.record_pack_files(r1["id"], files)
        assert recorded == 400

        # Verify pack release updated
        updated_rel = db.conn.execute(
            "SELECT * FROM pack_releases WHERE id = ?", (r1["id"],)
        ).fetchone()
        assert updated_rel["file_count"] == 400
        assert updated_rel["scan_status"] == "scanned"
        assert updated_rel["scanned_at"] is not None

        # Verify upsert never resets a scanned release back to pending
        r3 = db.upsert_pack_release(
            indexer_id="idx-tl",
            guid="guid-100",
            title="VA - Top 400 Pool May 2024",
            scan_status="pending",
        )
        assert r3["scan_status"] == "scanned"

        # 4. Search pack files
        # Search finds Titanium by David Guetta inside the 400-file pack
        matches = db.search_pack_files("David Guetta", "Titanium")
        assert len(matches) >= 2
        titles_found = [m.parsed_title for m in matches]
        assert "Titanium" in titles_found
        # Ignores Titanium (Live) when looking for Titanium
        assert "Titanium (Live)" not in titles_found
        # Multi-artist credit "David Guetta feat. Sia - Titanium" matches
        paths_found = [m.path for m in matches]
        assert "046 David Guetta feat. Sia - Titanium.flac" in paths_found

        # Can specifically search for Titanium (Live)
        live_matches = db.search_pack_files("David Guetta", "Titanium (Live)")
        assert len(live_matches) == 1
        assert live_matches[0].parsed_title == "Titanium (Live)"

        # Non-Latin search works
        non_latin = db.search_pack_files("宇多田ヒカル", "First Love")
        assert len(non_latin) == 1
        assert non_latin[0].parsed_artist == "宇多田ヒカル"
        assert non_latin[0].parsed_title == "First Love"

        # 5. Scanned-only requirement: a pending or failed pack is not returned in search
        r_pending = db.upsert_pack_release(
            indexer_id="idx-tl",
            guid="guid-unscanned",
            title="VA - Top Hits Unscanned",
            scan_status="pending",
        )
        # Manually insert files and FTS for r_pending without setting scanned
        db.conn.execute(
            "INSERT INTO pack_files (pack_id, file_index, path, size_bytes, ext, parsed_artist, parsed_title) "
            "VALUES (?, 0, 'David Guetta - Titanium.mp3', 8000000, '.mp3', 'David Guetta', 'Titanium')",
            (r_pending["id"],),
        )
        db.conn.execute(
            "INSERT INTO pack_files_fts (search, pack_id, file_index) VALUES (?, ?, 0)",
            ("david guetta titanium", r_pending["id"]),
        )
        db.conn.commit()
        # Search should not include r_pending because its scan_status is 'pending'
        matches_filtered = db.search_pack_files("David Guetta", "Titanium")
        for m in matches_filtered:
            assert m.pack_id != r_pending["id"]

        # 6. Mark failed and skipped
        db.mark_pack_failed(r_pending["id"], "Torrent file corrupted")
        failed_row = db.conn.execute(
            "SELECT scan_status, scan_error FROM pack_releases WHERE id = ?",
            (r_pending["id"],),
        ).fetchone()
        assert failed_row["scan_status"] == "failed"
        assert "Torrent file corrupted" in failed_row["scan_error"]

        db.mark_pack_skipped(r_pending["id"], "No audio files")
        skipped_row = db.conn.execute(
            "SELECT scan_status, scan_error FROM pack_releases WHERE id = ?",
            (r_pending["id"],),
        ).fetchone()
        assert skipped_row["scan_status"] == "skipped"
        assert "No audio files" in skipped_row["scan_error"]

        # 7. Stats
        stats = db.pack_index_stats()
        assert stats["packs"] == 2
        assert stats["scanned"] == 1
        assert stats["pending"] == 0
        assert stats["files"] >= 400

        # 8. Pruning
        # Add an old pack
        old_time = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d %H:%M:%S")
        r_old = db.upsert_pack_release(
            indexer_id="idx-tl",
            guid="guid-old",
            title="Old Pack",
            scan_status="scanned",
        )
        db.conn.execute(
            "UPDATE pack_releases SET created_at = ? WHERE id = ?", (old_time, r_old["id"])
        )
        db.conn.commit()

        pruned = db.prune_pack_index(max_age_days=90, max_packs=1000)
        assert pruned >= 1
        remaining_old = db.conn.execute(
            "SELECT 1 FROM pack_releases WHERE id = ?", (r_old["id"],)
        ).fetchone()
        assert remaining_old is None

        # 9. Indexer delete cascades
        db.conn.execute("PRAGMA foreign_keys = ON")
        db.delete_indexer("idx-tl")
        assert (
            db.conn.execute(
                "SELECT COUNT(*) FROM pack_releases WHERE indexer_id = 'idx-tl'"
            ).fetchone()[0]
            == 0
        )
        assert (
            db.conn.execute(
                "SELECT COUNT(*) FROM pack_files WHERE pack_id = ?", (r1["id"],)
            ).fetchone()[0]
            == 0
        )

    finally:
        db.close()
