"""Pack index store mixin for TrackSeerr database.

Provides persistence, queries, search, and lifecycle management for torrent pack releases and files.
"""

from __future__ import annotations

import logging
from pathlib import Path
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from trackseerr.exclusions import exclusion_norm
from trackseerr.lidarr_release import titles_match
from trackseerr.media_match import split_artist_credits
from trackseerr.pack_index import (
    AUDIO_EXTS,
    PackFileMatch,
    clean_pool_title,
    parse_pack_file,
    search_text,
)

logger = logging.getLogger("trackseerr.pack_index_store")


def _quote_fts(token: str) -> str:
    escaped = token.replace('"', '""')
    return f'"{escaped}"'


def _variant_qualifier_blocks(
    variant: str | None,
    edit_kind: str | None,
    wanted_title: str,
    wanted_variant: str | None = None,
) -> bool:
    """True if candidate and wanted title have incompatible variant/edit qualifiers."""
    cand_v = (variant or edit_kind or "").lower()
    has_cand_v = bool(variant or edit_kind in ("Quick Hit", "Short Edit"))

    if has_cand_v:
        if "intro" in cand_v and not re.search(r"(?i)\bintro\b", wanted_title):
            return True
        if ("acapella" in cand_v or "acappella" in cand_v) and not re.search(r"(?i)\bacap+ella\b", wanted_title):
            return True
        if ("instrumental" in cand_v or "inst" in cand_v) and not re.search(r"(?i)\binst(?:rumental)?\b", wanted_title):
            return True
        if ("dj tool" in cand_v or "tool" in cand_v) and not re.search(r"(?i)\b(?:dj\s*)?tool\b", wanted_title):
            return True
        if ("quick hit" in cand_v or "short edit" in cand_v) and not re.search(
            r"(?i)\b(?:quick\s*hit|short\s*edit)\b", wanted_title
        ):
            return True

    if wanted_variant:
        wv = wanted_variant.lower()
        if "intro" in wv and "intro" not in cand_v:
            return True
        if ("acapella" in wv or "acappella" in wv) and not ("acapella" in cand_v or "acappella" in cand_v):
            return True
        if ("instrumental" in wv or "inst" in wv) and not ("instrumental" in cand_v or "inst" in cand_v):
            return True
        if "tool" in wv and "tool" not in cand_v:
            return True
        if ("quick hit" in wv or "short edit" in wv) and not ("quick hit" in cand_v or "short edit" in cand_v):
            return True

    return False


def _match_sort_key(m: PackFileMatch, wanted_title: str) -> tuple[int, int, int, int]:
    clean_wanted = bool(re.search(r"(?i)\bclean\b", wanted_title))
    ek = (m.edit_kind or "").lower()
    if clean_wanted:
        dirty_score = 2 if "clean" in ek else 1
    else:
        if any(k in ek for k in ("dirty", "explicit")):
            dirty_score = 2
        elif "clean" in ek:
            dirty_score = 1
        else:
            dirty_score = 2

    extended_wanted = bool(re.search(r"(?i)\bextended\b", wanted_title))
    cand_text = f"{m.parsed_title or ''} {m.path}".lower()
    is_extended = "extended" in cand_text
    is_radio = "radio" in cand_text

    if extended_wanted:
        ver_score = 2 if is_extended else (1 if not is_radio else 0)
    else:
        ver_score = 2 if is_radio else (0 if is_extended else 1)

    seeders = m.seeders if m.seeders is not None else -1
    size_bytes = m.size_bytes if m.size_bytes is not None else -1
    return (dirty_score, ver_score, seeders, size_bytes)


def _artist_matches(target_artist: str, candidate_artist_or_path: str) -> bool:
    norm_target = exclusion_norm(target_artist)
    norm_cand = exclusion_norm(candidate_artist_or_path)
    if not norm_target or not norm_cand:
        return False
    if norm_target in norm_cand or norm_cand in norm_target:
        return True
    target_credits = [exclusion_norm(c) for c in split_artist_credits(target_artist) if exclusion_norm(c)]
    cand_credits = [exclusion_norm(c) for c in split_artist_credits(candidate_artist_or_path) if exclusion_norm(c)]
    for tc in target_credits:
        for cc in cand_credits:
            if tc == cc or tc in cc or cc in tc:
                return True
    return False


class PackIndexStoreMixin:
    """Mixin for Database providing pack releases and files storage operations."""

    def upsert_pack_release(
        self, indexer_id: str, guid: str, title: str, **fields: Any
    ) -> dict[str, Any]:
        """Insert a pack release or return existing. Never resets a 'scanned' row to pending."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM pack_releases WHERE indexer_id = ? AND guid = ?",
                (indexer_id, guid),
            )
            existing = cur.fetchone()
            if existing:
                row = dict(existing)
                update_cols: list[str] = []
                params: list[Any] = []
                for col in (
                    "title",
                    "download_url",
                    "magnet_url",
                    "info_hash",
                    "size_bytes",
                    "seeders",
                    "published_at",
                ):
                    if col in fields and fields[col] is not None:
                        update_cols.append(f"{col} = ?")
                        params.append(fields[col])
                if "scan_status" in fields and fields["scan_status"] is not None:
                    new_status = str(fields["scan_status"])
                    if not (row["scan_status"] == "scanned" and new_status == "pending"):
                        update_cols.append("scan_status = ?")
                        params.append(new_status)
                if update_cols:
                    params.extend([indexer_id, guid])
                    self.conn.execute(
                        f"UPDATE pack_releases SET {', '.join(update_cols)} WHERE indexer_id = ? AND guid = ?",
                        params,
                    )
                    self.conn.commit()
                    cur = self.conn.execute(
                        "SELECT * FROM pack_releases WHERE indexer_id = ? AND guid = ?",
                        (indexer_id, guid),
                    )
                    return dict(cur.fetchone())
                return row

            pack_id = f"pack-{uuid.uuid4().hex[:12]}"
            scan_status = str(fields.get("scan_status") or "pending")
            self.conn.execute(
                """
                INSERT INTO pack_releases (
                    id, indexer_id, guid, title, download_url, magnet_url, info_hash,
                    size_bytes, seeders, published_at, file_count, scan_status, scan_error, scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pack_id,
                    indexer_id,
                    guid,
                    title,
                    fields.get("download_url"),
                    fields.get("magnet_url"),
                    fields.get("info_hash"),
                    fields.get("size_bytes"),
                    fields.get("seeders"),
                    fields.get("published_at"),
                    int(fields.get("file_count") or 0),
                    scan_status,
                    fields.get("scan_error"),
                    fields.get("scanned_at"),
                ),
            )
            self.conn.commit()
            cur = self.conn.execute("SELECT * FROM pack_releases WHERE id = ?", (pack_id,))
            return dict(cur.fetchone())

    def list_pending_pack_releases(self, limit: int = 50) -> list[dict[str, Any]]:
        """List pending pack releases ordered oldest first."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM pack_releases WHERE scan_status = 'pending' ORDER BY created_at ASC, rowid ASC LIMIT ?",
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    def record_pack_files(self, pack_id: str, files: list[dict[str, Any]]) -> int:
        """Replace files for pack_id, update pack_releases metadata, index audio files in FTS."""
        with self._lock:
            self.conn.execute("DELETE FROM pack_files_fts WHERE pack_id = ?", (pack_id,))
            self.conn.execute("DELETE FROM pack_files WHERE pack_id = ?", (pack_id,))

            pack_files_data = []
            fts_data = []

            for f in files:
                f_idx = int(f["file_index"])
                f_path = str(f["path"])
                f_size = f.get("size_bytes")
                ext = Path(f_path.replace("\\", "/")).suffix.lower() if f_path else None
                artist, title = parse_pack_file(f_path)
                pack_files_data.append((pack_id, f_idx, f_path, f_size, ext, artist, title))
                if ext in AUDIO_EXTS:
                    _, extras = clean_pool_title(title or "")
                    fts_text = search_text(f_path, artist, title, extras=extras)
                    fts_data.append((fts_text, pack_id, f_idx))

            if pack_files_data:
                self.conn.executemany(
                    """
                    INSERT INTO pack_files (pack_id, file_index, path, size_bytes, ext, parsed_artist, parsed_title)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    pack_files_data,
                )

            if fts_data:
                self.conn.executemany(
                    "INSERT INTO pack_files_fts (search, pack_id, file_index) VALUES (?, ?, ?)",
                    fts_data,
                )

            self.conn.execute(
                """
                UPDATE pack_releases
                SET file_count = ?, scan_status = 'scanned', scanned_at = CURRENT_TIMESTAMP, scan_error = NULL
                WHERE id = ?
                """,
                (len(files), pack_id),
            )
            self.conn.commit()
            return len(files)

    def mark_pack_failed(self, pack_id: str, error: str) -> None:
        """Mark a pack release as failed with an error message."""
        with self._lock:
            self.conn.execute(
                """
                UPDATE pack_releases
                SET scan_status = 'failed', scan_error = ?, scanned_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (str(error), pack_id),
            )
            self.conn.commit()

    def mark_pack_skipped(self, pack_id: str, reason: str) -> None:
        """Mark a pack release as skipped with a reason."""
        with self._lock:
            self.conn.execute(
                """
                UPDATE pack_releases
                SET scan_status = 'skipped', scan_error = ?, scanned_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (str(reason), pack_id),
            )
            self.conn.commit()

    def search_pack_files(
        self, artist: str, title: str, limit: int = 50
    ) -> list[PackFileMatch]:
        """Search pack files matching artist and title."""
        title_norm = exclusion_norm(title)
        title_tokens = [tok for tok in title_norm.split() if len(tok) >= 2]

        artist_norm = exclusion_norm(artist)
        artist_tokens = [tok for tok in artist_norm.split() if len(tok) >= 2]
        first_artist_token = artist_tokens[0] if artist_tokens else None

        fts_tokens: list[str] = []
        seen: set[str] = set()
        for tok in title_tokens:
            if tok not in seen:
                seen.add(tok)
                fts_tokens.append(_quote_fts(tok))
        if first_artist_token and first_artist_token not in seen:
            fts_tokens.append(_quote_fts(first_artist_token))

        if not fts_tokens:
            return []

        match_query = " AND ".join(fts_tokens)

        wanted_clean, wanted_extras = clean_pool_title(title)
        if not wanted_clean:
            wanted_clean = title
        wanted_variant = wanted_extras.get("variant") or (
            wanted_extras.get("edit_kind")
            if wanted_extras.get("edit_kind") in ("Quick Hit", "Short Edit")
            else None
        )

        with self._lock:
            try:
                cur = self.conn.execute(
                    """
                    SELECT
                        pf.pack_id,
                        pf.file_index,
                        pf.path,
                        pf.size_bytes,
                        pf.ext,
                        pf.parsed_artist,
                        pf.parsed_title,
                        pr.title AS pack_title,
                        pr.indexer_id,
                        pr.download_url,
                        pr.magnet_url,
                        pr.info_hash,
                        pr.seeders,
                        pr.file_count
                    FROM pack_files_fts fts
                    JOIN pack_files pf ON pf.pack_id = fts.pack_id AND pf.file_index = fts.file_index
                    JOIN pack_releases pr ON pr.id = pf.pack_id
                    WHERE pack_files_fts MATCH ?
                      AND pr.scan_status = 'scanned'
                    """,
                    (match_query,),
                )
                rows = [dict(r) for r in cur.fetchall()]
            except sqlite3.OperationalError as exc:
                logger.warning("FTS search failed for query %r: %s", match_query, exc)
                return []

        results: list[PackFileMatch] = []
        for r in rows:
            stem = Path(r["path"].replace("\\", "/")).stem
            _, path_extras = clean_pool_title(stem)
            raw_title = r["parsed_title"] or stem
            cand_title, title_extras = clean_pool_title(raw_title)
            if not cand_title:
                cand_title = stem

            extras = {**path_extras, **title_extras}
            variant = extras.get("variant")
            edit_kind = extras.get("edit_kind")
            bpm = extras.get("bpm")

            # Check variant qualifier: intro / acapella / instrumental / dj tool / quick hit
            # Only matches when the wanted title has the same kind of qualifier
            if _variant_qualifier_blocks(variant, edit_kind, title, wanted_variant):
                continue

            if not titles_match(cand_title, wanted_clean):
                continue

            cand_artist_source = r["parsed_artist"] or r["path"]
            if not _artist_matches(artist, cand_artist_source):
                continue

            results.append(
                PackFileMatch(
                    pack_id=r["pack_id"],
                    file_index=r["file_index"],
                    path=r["path"],
                    size_bytes=r["size_bytes"],
                    ext=r["ext"],
                    parsed_artist=r["parsed_artist"],
                    parsed_title=cand_title,
                    pack_title=r["pack_title"],
                    indexer_id=r["indexer_id"],
                    download_url=r["download_url"],
                    magnet_url=r["magnet_url"],
                    info_hash=r["info_hash"],
                    seeders=r["seeders"],
                    file_count=r["file_count"],
                    variant=variant,
                    edit_kind=edit_kind,
                    bpm=bpm,
                )
            )

        results.sort(key=lambda m: _match_sort_key(m, title), reverse=True)
        return results[:limit]

    def prune_pack_index(self, max_age_days: int = 90, max_packs: int = 20000) -> int:
        """Prune old and excess pack releases and their FTS entries (oldest first)."""
        with self._lock:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            cur = self.conn.execute(
                "SELECT id FROM pack_releases WHERE created_at < ? ORDER BY created_at ASC",
                (cutoff,),
            )
            aged_ids = [r[0] for r in cur.fetchall()]

            total = self.conn.execute("SELECT COUNT(*) FROM pack_releases").fetchone()[0]
            excess_count = max(0, total - max_packs)
            excess_ids: list[str] = []
            if excess_count > 0:
                cur = self.conn.execute(
                    "SELECT id FROM pack_releases ORDER BY created_at ASC, rowid ASC LIMIT ?",
                    (excess_count,),
                )
                excess_ids = [r[0] for r in cur.fetchall()]

            to_delete = list(dict.fromkeys(aged_ids + excess_ids))
            if to_delete:
                for i in range(0, len(to_delete), 500):
                    chunk = to_delete[i : i + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    self.conn.execute(
                        f"DELETE FROM pack_files_fts WHERE pack_id IN ({placeholders})", chunk
                    )
                    self.conn.execute(
                        f"DELETE FROM pack_releases WHERE id IN ({placeholders})", chunk
                    )
                self.conn.commit()
            return len(to_delete)

    def pack_index_stats(self) -> dict[str, int]:
        """Return pack index summary statistics."""
        with self._lock:
            packs = self.conn.execute("SELECT COUNT(*) FROM pack_releases").fetchone()[0]
            scanned = self.conn.execute(
                "SELECT COUNT(*) FROM pack_releases WHERE scan_status = 'scanned'"
            ).fetchone()[0]
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM pack_releases WHERE scan_status = 'pending'"
            ).fetchone()[0]
            failed = self.conn.execute(
                "SELECT COUNT(*) FROM pack_releases WHERE scan_status = 'failed'"
            ).fetchone()[0]
            files = self.conn.execute("SELECT COUNT(*) FROM pack_files").fetchone()[0]
            return {
                "packs": int(packs),
                "scanned": int(scanned),
                "pending": int(pending),
                "failed": int(failed),
                "files": int(files),
            }

    def reparse_pack_files(self, batch_size: int = 500) -> int:
        """One-off recomputation of parsed_artist, parsed_title, and FTS for already-indexed packs."""
        with self._lock:
            if getattr(self, "get_kv", None) and self.get_kv("pack_names_v2"):
                return 0

            cur = self.conn.execute("SELECT pack_id, file_index, path, ext FROM pack_files")
            rows = cur.fetchall()
            total = 0
            for i in range(0, len(rows), batch_size):
                chunk = rows[i : i + batch_size]
                update_files = []
                fts_delete = []
                fts_insert = []
                for r in chunk:
                    p_id, f_idx, f_path, ext = r[0], r[1], r[2], r[3]
                    artist, title = parse_pack_file(f_path)
                    update_files.append((artist, title, p_id, f_idx))
                    if ext in AUDIO_EXTS:
                        fts_delete.append((p_id, f_idx))
                        _, extras = clean_pool_title(title or "")
                        fts_text = search_text(f_path, artist, title, extras=extras)
                        fts_insert.append((fts_text, p_id, f_idx))

                if update_files:
                    self.conn.executemany(
                        "UPDATE pack_files SET parsed_artist = ?, parsed_title = ? WHERE pack_id = ? AND file_index = ?",
                        update_files,
                    )
                if fts_delete:
                    self.conn.executemany(
                        "DELETE FROM pack_files_fts WHERE pack_id = ? AND file_index = ?",
                        fts_delete,
                    )
                if fts_insert:
                    self.conn.executemany(
                        "INSERT INTO pack_files_fts (search, pack_id, file_index) VALUES (?, ?, ?)",
                        fts_insert,
                    )
                self.conn.commit()
                total += len(chunk)

            if getattr(self, "set_kv", None):
                self.set_kv("pack_names_v2", "1")
            return total
