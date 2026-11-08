"""Tests for torrent pack index parsing, logic, matching, and database storage."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import threading
import time

from trackseerr.pack_index import (
    clean_pool_title,
    infer_audio_quality,
    looks_like_pack,
    parse_pack_file,
    search_text,
)
from trackseerr.pack_scanner import get_pack_backfill_pages
from trackseerr.quality import parse_release_title
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
        cur_pf = db.conn.execute(
            "INSERT INTO pack_files (pack_id, file_index, path, size_bytes, ext, parsed_artist, parsed_title) "
            "VALUES (?, 0, 'David Guetta - Titanium.mp3', 8000000, '.mp3', 'David Guetta', 'Titanium')",
            (r_pending["id"],),
        )
        db.conn.execute(
            "INSERT INTO pack_files_fts (rowid, search, pack_id, file_index) VALUES (?, ?, ?, 0)",
            (cur_pf.lastrowid, "david guetta titanium", r_pending["id"]),
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


def test_clean_pool_title() -> None:
    # 1. Live evidence: remix kept, bpm, edit_kind, featured extracted
    title, extras = clean_pool_title("Titanium Ft Sia (Alesso Remix) (Clean) 126")
    assert title == "Titanium (Alesso Remix)"
    assert extras["bpm"] == 126
    assert extras["edit_kind"] == "Clean"
    assert extras["featured"] == "Sia"

    # 2. Key and BPM trailing, Dirty edit
    title, extras = clean_pool_title("Titanium Ft Sia (Dirty) 126 8A")
    assert title == "Titanium"
    assert extras["bpm"] == 126
    assert extras["key"] == "8A"
    assert extras["edit_kind"] == "Dirty"
    assert extras["featured"] == "Sia"

    # 3. Variants: Intro Clean, Acapella, Instrumental, DJ Tool, Quick Hit
    t, e = clean_pool_title("Song (Intro Clean) 128 4A [www.djpool.com]")
    assert t == "Song"
    assert e["variant"] == "Intro Clean"
    assert e["edit_kind"] == "Clean"
    assert e["bpm"] == 128
    assert e["key"] == "4A"

    t, e = clean_pool_title("Song (Acapella) 124")
    assert t == "Song"
    assert e["variant"] == "Acapella"

    t, e = clean_pool_title("Song (Instrumental)")
    assert t == "Song"
    assert e["variant"] == "Instrumental"

    t, e = clean_pool_title("Song (Inst)")
    assert t == "Song"
    assert e["variant"] == "Instrumental"

    t, e = clean_pool_title("Song (DJ Tool)")
    assert t == "Song"
    assert e["variant"] == "DJ Tool"

    t, e = clean_pool_title("Song (Quick Hit) 120")
    assert t == "Song"
    assert e["variant"] == "Quick Hit"
    assert e["edit_kind"] == "Quick Hit"

    t, e = clean_pool_title("Song (Short Edit)")
    assert t == "Song"
    assert e["variant"] == "Short Edit"

    t, e = clean_pool_title("Song (Clean Edit)")
    assert t == "Song"
    assert e["edit_kind"] == "Clean Edit"

    t, e = clean_pool_title("Song (Dirty Edit)")
    assert t == "Song"
    assert e["edit_kind"] == "Dirty Edit"

    # 4. Keep mixes like (Extended Mix), (Radio Edit)
    t, _ = clean_pool_title("Song (Extended Mix)")
    assert t == "Song (Extended Mix)"

    t, _ = clean_pool_title("Song (Radio Edit)")
    assert t == "Song (Radio Edit)"


def test_parse_pack_file_with_dj_pool_names() -> None:
    # Featured Sia extracted and appended to artist, title cleaned
    artist, title = parse_pack_file("David Guetta - Titanium Ft Sia (Dirty) 126 8A.mp3")
    assert artist == "David Guetta feat. Sia"
    assert title == "Titanium"

    # Evidence file: remix preserved in title
    artist, title = parse_pack_file("David Guetta - Titanium Ft Sia (Alesso Remix) (Clean) 126.mp3")
    assert artist == "David Guetta feat. Sia"
    assert title == "Titanium (Alesso Remix)"


def test_infer_audio_quality() -> None:
    # FLAC
    assert infer_audio_quality(".flac", "01 Song.flac", "Pack Title") == "FLAC"
    p = parse_release_title(infer_audio_quality(".flac", "01 Song.flac", "Pack Title"))
    assert p.quality == "FLAC 16bit"

    # MP3 320 in file name
    assert infer_audio_quality(".mp3", "Artist - Song 320.mp3", "Pack Title") == "MP3 320"
    p = parse_release_title(infer_audio_quality(".mp3", "Artist - Song 320.mp3", "Pack Title"))
    assert p.quality == "MP3 320"
    assert p.bitrate_kbps == 320

    # MP3 320 in pack title
    assert infer_audio_quality(".mp3", "Artist - Song.mp3", "Dj Pools Music Pack [09 Aug 2026] 320kbps") == "MP3 320"

    # MP3 256, 192, V0
    assert infer_audio_quality(".mp3", "Song 256k.mp3", "Pack") == "MP3 256"
    assert infer_audio_quality(".mp3", "Song 192kbps.mp3", "Pack") == "MP3 192"
    assert infer_audio_quality(".mp3", "Song V0.mp3", "Pack") == "MP3 V0"

    # Plain MP3 without bitrate
    assert infer_audio_quality(".mp3", "Song.mp3", "Pack") == "MP3"

    # AAC, WAV, AIFF
    assert infer_audio_quality(".m4a", "Song.m4a", "Pack") == "AAC"
    assert infer_audio_quality(".aac", "Song.aac", "Pack") == "AAC"
    assert infer_audio_quality(".wav", "Song.wav", "Pack") == "WAV"
    assert infer_audio_quality(".aiff", "Song.aiff", "Pack") == "AIFF"


def test_evidence_file_matching_and_variants(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "evidence_test.db"))
    try:
        idx = db.create_indexer(
            {"id": "idx-ev", "name": "PoolIndexer", "indexer_type": "torznab", "host_url": "https://pool.org"}
        )
        pr = db.upsert_pack_release(
            indexer_id=idx["id"],
            guid="guid-ev",
            title="Dj Pools Music Pack [09 Aug 2026] 320kbps",
            scan_status="scanned",
            seeders=20,
        )
        files = [
            {"file_index": 0, "path": "David Guetta - Titanium Ft Sia (Alesso Remix) (Clean) 126.mp3", "size_bytes": 8000000},
            {"file_index": 1, "path": "David Guetta - Titanium Ft Sia (Dirty) 126 8A.mp3", "size_bytes": 8100000},
            {"file_index": 2, "path": "David Guetta - Titanium (Clean) 126.mp3", "size_bytes": 8000000},
            {"file_index": 3, "path": "David Guetta - Titanium (Intro Clean) 128 4A.mp3", "size_bytes": 9000000},
            {"file_index": 4, "path": "David Guetta - Titanium (Acapella) 126.mp3", "size_bytes": 6000000},
            {"file_index": 5, "path": "David Guetta - Titanium (Instrumental).mp3", "size_bytes": 7500000},
            {"file_index": 6, "path": "David Guetta - Titanium (Quick Hit) 124.mp3", "size_bytes": 4000000},
            {"file_index": 7, "path": "David Guetta - Titanium (Extended Mix).mp3", "size_bytes": 10000000},
        ]
        db.record_pack_files(pr["id"], files)

        # 1. Plain "Titanium":
        # Evidence remix file does NOT match plain "Titanium"
        matches = db.search_pack_files("David Guetta", "Titanium")
        matched_paths = [m.path for m in matches]
        assert "David Guetta - Titanium Ft Sia (Alesso Remix) (Clean) 126.mp3" not in matched_paths
        # David Guetta - Titanium Ft Sia (Dirty) 126 8A.mp3 DOES match plain Titanium
        assert "David Guetta - Titanium Ft Sia (Dirty) 126 8A.mp3" in matched_paths
        # Intro, Acapella, Instrumental, Quick Hit do NOT match plain Titanium
        assert "David Guetta - Titanium (Intro Clean) 128 4A.mp3" not in matched_paths
        assert "David Guetta - Titanium (Acapella) 126.mp3" not in matched_paths
        assert "David Guetta - Titanium (Instrumental).mp3" not in matched_paths
        assert "David Guetta - Titanium (Quick Hit) 124.mp3" not in matched_paths

        # Clean/Dirty preference: Dirty comes before Clean
        dirty_idx = matched_paths.index("David Guetta - Titanium Ft Sia (Dirty) 126 8A.mp3")
        clean_idx = matched_paths.index("David Guetta - Titanium (Clean) 126.mp3")
        assert dirty_idx < clean_idx

        # 2. Evidence remix matches when explicitly wanted
        remix_matches = db.search_pack_files("David Guetta", "Titanium (Alesso Remix)")
        assert len(remix_matches) == 1
        assert remix_matches[0].path == "David Guetta - Titanium Ft Sia (Alesso Remix) (Clean) 126.mp3"
        assert remix_matches[0].parsed_title == "Titanium (Alesso Remix)"
        assert remix_matches[0].edit_kind == "Clean"
        assert remix_matches[0].bpm == 126

        # 3. Variants match when asked
        intro_matches = db.search_pack_files("David Guetta", "Titanium (Intro)")
        assert any(m.path == "David Guetta - Titanium (Intro Clean) 128 4A.mp3" for m in intro_matches)

        acap_matches = db.search_pack_files("David Guetta", "Titanium (Acapella)")
        assert any(m.path == "David Guetta - Titanium (Acapella) 126.mp3" for m in acap_matches)

        inst_matches = db.search_pack_files("David Guetta", "Titanium (Instrumental)")
        assert any(m.path == "David Guetta - Titanium (Instrumental).mp3" for m in inst_matches)

        qh_matches = db.search_pack_files("David Guetta", "Titanium (Quick Hit)")
        assert any(m.path == "David Guetta - Titanium (Quick Hit) 124.mp3" for m in qh_matches)

        # 4. Extended preferred over Radio when wanted asks for Extended
        ext_matches = db.search_pack_files("David Guetta", "Titanium (Extended Mix)")
        assert len(ext_matches) >= 1
        assert ext_matches[0].path == "David Guetta - Titanium (Extended Mix).mp3"

    finally:
        db.close()


def test_reparse_pack_files_marker(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "reparse_test.db"))
    try:
        idx = db.create_indexer(
            {"id": "idx-rp", "name": "Indexer", "indexer_type": "torznab", "host_url": "https://test.org"}
        )
        pr = db.upsert_pack_release(
            indexer_id=idx["id"],
            guid="guid-rp",
            title="Pack 320",
            scan_status="scanned",
        )
        # Directly insert old-format uncleaned row
        db.conn.execute(
            """
            INSERT INTO pack_files (pack_id, file_index, path, size_bytes, ext, parsed_artist, parsed_title)
            VALUES (?, 0, 'David Guetta - Titanium Ft Sia (Alesso Remix) (Clean) 126.mp3', 8000000, '.mp3', 'David Guetta', 'Titanium Ft Sia (Alesso Remix) (Clean) 126')
            """,
            (pr["id"],),
        )
        db.conn.execute(
            "INSERT INTO pack_files_fts (search, pack_id, file_index) VALUES ('david guetta titanium', ?, 0)",
            (pr["id"],),
        )
        db.conn.commit()

        # Marker is not yet set
        assert db.get_kv("pack_names_v3") is None

        # Even if v2 was set, v3 missing causes rebuild
        db.set_kv("pack_names_v2", "1")

        # First run of reparse updates rows and sets marker
        reparsed = db.reparse_pack_files()
        assert reparsed == 1
        assert db.get_kv("pack_names_v3") == "1"

        # Verify row updated
        row = db.conn.execute(
            "SELECT parsed_artist, parsed_title FROM pack_files WHERE pack_id = ? AND file_index = 0",
            (pr["id"],),
        ).fetchone()
        assert row["parsed_artist"] == "David Guetta feat. Sia"
        assert row["parsed_title"] == "Titanium (Alesso Remix)"

        # Second run returns 0 and does not reparse
        second_run = db.reparse_pack_files()
        assert second_run == 0

    finally:
        db.close()


def test_pack_backfill_pages_setting(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "backfill_pages.db"))
    try:
        # Default is 5
        assert get_pack_backfill_pages(db) == 5

        # Custom value
        db.set_kv("pack_backfill_pages", "10")
        assert get_pack_backfill_pages(db) == 10

        # Clamped low to 1
        db.set_kv("pack_backfill_pages", "0")
        assert get_pack_backfill_pages(db) == 1

        # Clamped high to 50
        db.set_kv("pack_backfill_pages", "100")
        assert get_pack_backfill_pages(db) == 50

        # Invalid string defaults to 5
        db.set_kv("pack_backfill_pages", "invalid")
        assert get_pack_backfill_pages(db) == 5

    finally:
        db.close()


def test_reparse_20k_synthetic_files_perf_and_rowids(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "perf_20k.db"))
    try:
        idx = db.create_indexer(
            {"id": "idx-perf", "name": "Indexer", "indexer_type": "torznab", "host_url": "https://test.org"}
        )
        pr = db.upsert_pack_release(
            indexer_id=idx["id"],
            guid="guid-perf",
            title="Synthetic 20k Pack",
            scan_status="scanned",
        )
        pack_id = pr["id"]

        # Insert 20,000 synthetic files: 15,000 audio (.mp3), 5,000 non-audio (.txt)
        batch = []
        for i in range(20000):
            if i % 4 != 0:
                ext = ".mp3"
                path = f"Artist {i % 100} - Song {i}.mp3"
            else:
                ext = ".txt"
                path = f"Info {i}.txt"
            batch.append((pack_id, i, path, 5000000, ext, None, None))

        with db._lock:
            db.conn.executemany(
                """
                INSERT INTO pack_files (pack_id, file_index, path, size_bytes, ext, parsed_artist, parsed_title)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                batch,
            )
            # Add dummy FTS row to verify it gets wiped before rebuild
            db.conn.execute("INSERT INTO pack_files_fts (search, pack_id, file_index) VALUES ('old', 'old', 0)")
            db.conn.commit()

        t0 = time.perf_counter()
        reparsed = db.reparse_pack_files()
        duration = time.perf_counter() - t0

        assert reparsed == 20000
        assert duration < 5.0, f"Rebuild took {duration:.2f}s, expected < 5.0s"
        assert db.get_kv("pack_names_v3") == "1"

        # Assert FTS row count matches exactly audio files count
        audio_count = db.conn.execute(
            "SELECT COUNT(*) FROM pack_files WHERE ext = '.mp3'"
        ).fetchone()[0]
        fts_count = db.conn.execute("SELECT COUNT(*) FROM pack_files_fts").fetchone()[0]
        assert fts_count == audio_count == 15000

        # Assert all FTS rows have rowids that match pack_files
        matching = db.conn.execute(
            """
            SELECT COUNT(*) FROM pack_files_fts fts
            JOIN pack_files pf ON pf.rowid = fts.rowid
            WHERE pf.ext = '.mp3'
            """
        ).fetchone()[0]
        assert matching == 15000

        # Verify no orphan FTS rows
        orphans = db.conn.execute(
            """
            SELECT COUNT(*) FROM pack_files_fts fts
            LEFT JOIN pack_files pf ON pf.rowid = fts.rowid
            WHERE pf.rowid IS NULL
            """
        ).fetchone()[0]
        assert orphans == 0
    finally:
        db.close()


def test_reparse_lock_interleaving_probe_thread(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "lock_probe.db"))
    try:
        idx = db.create_indexer(
            {"id": "idx-probe", "name": "Indexer", "indexer_type": "torznab", "host_url": "https://test.org"}
        )
        pr = db.upsert_pack_release(
            indexer_id=idx["id"],
            guid="guid-probe",
            title="Synthetic Probe Pack",
            scan_status="scanned",
        )
        pack_id = pr["id"]

        # Insert 6,000 files to ensure rebuild takes several batches
        batch = [
            (pack_id, i, f"Artist {i % 50} - Track {i}.mp3", 4000000, ".mp3", None, None)
            for i in range(6000)
        ]
        with db._lock:
            db.conn.executemany(
                """
                INSERT INTO pack_files (pack_id, file_index, path, size_bytes, ext, parsed_artist, parsed_title)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                batch,
            )
            db.conn.commit()

        probe_acquired = threading.Event()
        probe_duration: list[float] = []

        def probe_fn() -> None:
            time.sleep(0.02)
            t_start = time.perf_counter()
            with db._lock:
                dt = time.perf_counter() - t_start
                probe_duration.append(dt)
                probe_acquired.set()

        t_probe = threading.Thread(target=probe_fn)
        t_rebuild = threading.Thread(target=db.reparse_pack_files)

        t_rebuild.start()
        t_probe.start()

        t_probe.join(timeout=3.0)
        t_rebuild.join(timeout=5.0)

        assert probe_acquired.is_set(), "Probe thread failed to acquire lock"
        assert probe_duration[0] < 0.5, f"Probe lock acquisition took {probe_duration[0]:.3f}s, expected < 0.5s"
    finally:
        db.close()


def test_record_pack_files_rescan_isolation(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "rescan_isolation.db"))
    try:
        idx = db.create_indexer(
            {"id": "idx-iso", "name": "Indexer", "indexer_type": "torznab", "host_url": "https://test.org"}
        )
        pr_a = db.upsert_pack_release(indexer_id=idx["id"], guid="guid-a", title="Pack A", scan_status="pending")
        pr_b = db.upsert_pack_release(indexer_id=idx["id"], guid="guid-b", title="Pack B", scan_status="pending")

        files_a = [
            {"file_index": 0, "path": "Daft Punk - One More Time.mp3", "size_bytes": 1000},
            {"file_index": 1, "path": "Daft Punk - Aerodynamic.mp3", "size_bytes": 2000},
        ]
        files_b = [
            {"file_index": 0, "path": "Justice - Genesis.mp3", "size_bytes": 3000},
            {"file_index": 1, "path": "Justice - Phantom.mp3", "size_bytes": 4000},
        ]

        db.record_pack_files(pr_a["id"], files_a)
        db.record_pack_files(pr_b["id"], files_b)

        # Snapshot Pack B's FTS rows and rowids
        b_fts_rows = db.conn.execute(
            "SELECT rowid, search, pack_id, file_index FROM pack_files_fts WHERE pack_id = ? ORDER BY file_index",
            (pr_b["id"],),
        ).fetchall()
        assert len(b_fts_rows) == 2

        # Re-scan Pack A with updated file list
        files_a_updated = [
            {"file_index": 0, "path": "Daft Punk - One More Time (Club Mix).mp3", "size_bytes": 1500},
            {"file_index": 1, "path": "Daft Punk - Harder Better Faster Stronger.mp3", "size_bytes": 2500},
            {"file_index": 2, "path": "Daft Punk - Crescendolls.mp3", "size_bytes": 3500},
        ]
        db.record_pack_files(pr_a["id"], files_a_updated)

        # Assert Pack B's FTS rows were completely untouched
        b_fts_rows_after = db.conn.execute(
            "SELECT rowid, search, pack_id, file_index FROM pack_files_fts WHERE pack_id = ? ORDER BY file_index",
            (pr_b["id"],),
        ).fetchall()
        assert [tuple(r) for r in b_fts_rows_after] == [tuple(r) for r in b_fts_rows]

        # Assert Pack A's FTS rows reflect the update with matching rowids
        a_fts_rows = db.conn.execute(
            """
            SELECT fts.rowid, pf.rowid, fts.file_index, pf.file_index
            FROM pack_files_fts fts
            JOIN pack_files pf ON pf.rowid = fts.rowid
            WHERE fts.pack_id = ?
            ORDER BY fts.file_index
            """,
            (pr_a["id"],),
        ).fetchall()
        assert len(a_fts_rows) == 3
        for r in a_fts_rows:
            assert r[0] == r[1]
    finally:
        db.close()


def test_prune_removes_fts_rows_by_rowid(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "prune_rowid.db"))
    try:
        idx = db.create_indexer(
            {"id": "idx-prune", "name": "Indexer", "indexer_type": "torznab", "host_url": "https://test.org"}
        )
        pr_old = db.upsert_pack_release(indexer_id=idx["id"], guid="guid-old", title="Old Pack", scan_status="pending")
        pr_new = db.upsert_pack_release(indexer_id=idx["id"], guid="guid-new", title="New Pack", scan_status="pending")

        db.record_pack_files(
            pr_old["id"],
            [{"file_index": 0, "path": "Old Artist - Old Song.mp3", "size_bytes": 1000}],
        )
        db.record_pack_files(
            pr_new["id"],
            [{"file_index": 0, "path": "New Artist - New Song.mp3", "size_bytes": 2000}],
        )

        # Backdate pr_old created_at
        old_date = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%d %H:%M:%S")
        with db._lock:
            db.conn.execute("UPDATE pack_releases SET created_at = ? WHERE id = ?", (old_date, pr_old["id"]))
            db.conn.commit()

        new_fts_rowid = db.conn.execute(
            "SELECT rowid FROM pack_files_fts WHERE pack_id = ?", (pr_new["id"],)
        ).fetchone()[0]

        pruned = db.prune_pack_index(max_age_days=30)
        assert pruned == 1

        # Old pack FTS entry is gone
        old_fts = db.conn.execute(
            "SELECT COUNT(*) FROM pack_files_fts WHERE pack_id = ?", (pr_old["id"],)
        ).fetchone()[0]
        assert old_fts == 0

        # New pack FTS entry is retained with same rowid
        new_fts = db.conn.execute(
            "SELECT rowid FROM pack_files_fts WHERE pack_id = ?", (pr_new["id"],)
        ).fetchone()
        assert new_fts is not None
        assert new_fts[0] == new_fts_rowid
    finally:
        db.close()
