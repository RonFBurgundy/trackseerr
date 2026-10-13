"""Synthetic large-catalog builder shared by the library perf tests and the benchmark script."""

from __future__ import annotations

import random
import sqlite3
from typing import Any

from trackseerr import library_track_index
from trackseerr.list_index import fold_search_text, library_sort_key
from trackseerr.storage import Database, clean_library_name

ARTISTS = 3000
ALBUMS = 30000
TRACKS = 317000
FILES = 30000
TRACKS_WITH_FILES = 20000

_SYLLABLES = (
    "an el on ri ta mo ve lu sa ke di no pa ra zu fi gro the ber lin dor ska mar tel ion eau ø é ü".split()
)


def _word(rng: random.Random) -> str:
    return "".join(rng.choice(_SYLLABLES) for _ in range(rng.randint(1, 3))).capitalize()


def _name(rng: random.Random, vocab: list[str], words: int) -> str:
    return " ".join(rng.choice(vocab) for _ in range(words))


def build_catalog(db: Database, scale: float = 1.0, seed: int = 7) -> dict[str, Any]:
    """Bulk-inserts a synthetic catalog (``scale`` 1.0 = 3k artists / 30k albums / 317k tracks / 30k files).

    Returns the counts plus a 3-letter and a 10-letter needle that occur in the data.
    """
    artists, albums, tracks = int(ARTISTS * scale), int(ALBUMS * scale), int(TRACKS * scale)
    files, files_tracks = int(FILES * scale), int(TRACKS_WITH_FILES * scale)
    rng = random.Random(seed)
    # Bulk load without the per-row triggers and the six new composite indexes; both are installed afterwards
    # (the denormalized columns are written directly, the trigram table is rebuilt by the migration's own code).
    cur = db.conn.cursor()
    library_track_index.drop_track_triggers(cur)
    for sql in library_track_index.INDEX_DDL:
        cur.execute("DROP INDEX IF EXISTS " + sql.split(" ON ")[0].split()[-1])
    vocab = [_word(rng) for _ in range(4000)]
    conn: sqlite3.Connection = db.conn

    def folded(text: str) -> tuple[str, str, str]:
        clean = clean_library_name(text)
        return text, clean, library_sort_key(text)

    artist_rows = []
    for i in range(artists):
        name = _name(rng, vocab, rng.randint(1, 2))
        _t, clean, sort = folded(name)
        artist_rows.append(
            (f"ar{i}", name, clean, sort, fold_search_text(name), fold_search_text(clean), rng.randint(0, 100))
        )
    conn.executemany(
        "INSERT INTO library_artists (id, name, clean_name, sort_name, search_text, search_clean, popularity) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(a[0], a[1], a[2], a[3], a[4], a[5], a[6]) for a in artist_rows],
    )

    album_rows = []
    album_artist = []
    album_info: list[tuple[str, str]] = []
    for i in range(albums):
        aid = rng.randrange(artists)
        title = _name(rng, vocab, rng.randint(1, 3))
        _t, clean, sort = folded(title)
        year = rng.randint(1960, 2025)
        date = f"{year}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}" if rng.random() < 0.6 else None
        album_artist.append(aid)
        album_info.append((sort, date if date else str(year)))
        album_rows.append(
            (f"al{i}", f"ar{aid}", title, clean, sort, fold_search_text(title), fold_search_text(clean), year, date)
        )
    conn.executemany(
        "INSERT INTO library_albums (id, artist_id, title, clean_title, sort_title, search_text, search_clean, "
        "year, release_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        album_rows,
    )

    track_rows = []
    for i in range(tracks):
        alb = rng.randrange(albums)
        title = _name(rng, vocab, rng.randint(1, 4))
        _t, clean, sort = folded(title)
        created = f"2024-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d} {rng.randint(0, 23):02d}:00:00"
        track_rows.append(
            (f"t{i}", f"al{alb}", f"ar{album_artist[alb]}", title, clean, sort,
             fold_search_text(title), fold_search_text(clean), i % 14 + 1, created,
             artist_rows[album_artist[alb]][3], album_info[alb][0], album_info[alb][1])
        )
    conn.executemany(
        "INSERT INTO library_tracks (id, album_id, artist_id, title, clean_title, sort_title, search_text, "
        "search_clean, track_number, created_at, artist_sort, album_sort, album_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        track_rows,
    )

    file_tracks = rng.sample(range(tracks), files_tracks)
    file_rows = []
    for i in range(files):
        tid = file_tracks[i] if i < files_tracks else rng.choice(file_tracks)
        file_rows.append(
            (f"f{i}", f"t{tid}", f"/music/{i}.flac", f"{i}.flac", "FLAC", "Lossless", rng.randint(1_000_000, 60_000_000))
        )
    conn.executemany(
        "INSERT INTO library_files (id, track_id, file_path, relative_path, codec, quality_name, size_bytes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        file_rows,
    )
    conn.execute(
        "UPDATE library_tracks SET has_file = 1, "
        "max_size_bytes = (SELECT MAX(size_bytes) FROM library_files WHERE track_id = library_tracks.id) "
        "WHERE id IN (SELECT track_id FROM library_files)"
    )
    library_track_index.rebuild_track_fts(cur)
    library_track_index.create_track_indexes(cur)
    library_track_index.create_track_triggers(cur)
    conn.commit()
    return {"tracks": tracks, "file_tracks": files_tracks, "word3": vocab[0][:3].lower(), "word10": track_rows[5][6][:10]}
