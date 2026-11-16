"""Unit tests for track_file_selection (choosing a single track file from an album .torrent)."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from trackseerr.clients.acquisition.torznab import IndexerUnavailable
from trackseerr.track_file_selection import fetch_torrent_bytes, select_track_file


def _bencode(val: Any) -> bytes:
    if isinstance(val, int):
        return f"i{val}e".encode("ascii")
    if isinstance(val, bytes):
        return f"{len(val)}:".encode("ascii") + val
    if isinstance(val, str):
        raw = val.encode("utf-8")
        return f"{len(raw)}:".encode("ascii") + raw
    if isinstance(val, list):
        return b"l" + b"".join(_bencode(item) for item in val) + b"e"
    if isinstance(val, dict):
        items = sorted(
            ((k.encode("utf-8") if isinstance(k, str) else k), v)
            for k, v in val.items()
        )
        return b"d" + b"".join(_bencode(k) + _bencode(v) for k, v in items) + b"e"
    raise TypeError(f"Unsupported bencode type: {type(val)}")


def _make_album_torrent(
    files: list[tuple[str, int]],
    name: str = "Artist - Album (2026) [FLAC]",
) -> bytes:
    file_entries = [
        {"length": size, "path": rel_path.split("/")}
        for rel_path, size in files
    ]
    return _bencode(
        {
            "info": {
                "files": file_entries,
                "name": name,
                "piece length": 16384,
                "pieces": b"0" * 20,
            }
        }
    )


def test_selects_matching_track_from_album() -> None:
    torrent_bytes = _make_album_torrent(
        [
            ("01 - Intro.flac", 15_000_000),
            ("02 - Liquid Hook (Zentura Remix).flac", 42_000_000),
            ("cover.jpg", 500_000),
        ]
    )

    entry, outcome = select_track_file(
        torrent_bytes,
        wanted_title="Liquid Hook (Zentura Remix)",
        artist="Ace Ventura",
    )

    assert outcome == "selected"
    assert entry is not None
    assert entry["file_index"] == 1
    assert entry["path"].endswith("02 - Liquid Hook (Zentura Remix).flac")
    assert entry["size_bytes"] == 42_000_000


def test_prefers_plain_over_radio_edit() -> None:
    torrent_bytes = _make_album_torrent(
        [
            ("01 - Midnight City (Radio Edit).flac", 22_000_000),
            ("02 - Midnight City.flac", 31_000_000),
            ("03 - Outro.flac", 18_000_000),
        ]
    )

    entry, outcome = select_track_file(
        torrent_bytes,
        wanted_title="Midnight City",
        artist="M83",
    )

    assert outcome == "selected"
    assert entry is not None
    assert entry["file_index"] == 1
    assert entry["path"].endswith("02 - Midnight City.flac")


def test_no_match_and_ambiguous() -> None:
    no_match_bytes = _make_album_torrent(
        [
            ("01 - Track One.flac", 20_000_000),
            ("02 - Track Two.flac", 25_000_000),
        ]
    )
    entry, outcome = select_track_file(
        no_match_bytes,
        wanted_title="Nonexistent Song",
        artist="Artist",
    )
    assert entry is None
    assert outcome == "no_match"

    ambiguous_bytes = _make_album_torrent(
        [
            ("CD1/01 - Track One.flac", 20_000_000),
            ("CD2/01 - Track One (2020 Remaster).flac", 22_000_000),
        ]
    )
    entry_amb, outcome_amb = select_track_file(
        ambiguous_bytes,
        wanted_title="Track One",
        artist="Artist",
    )
    assert entry_amb is None
    assert outcome_amb == "ambiguous"


def test_single_audio_file_is_not_partial() -> None:
    single_audio_bytes = _make_album_torrent(
        [
            ("01 - Only Song.flac", 30_000_000),
            ("cover.jpg", 400_000),
            ("release.log", 12_000),
        ]
    )
    entry, outcome = select_track_file(
        single_audio_bytes,
        wanted_title="Only Song",
        artist="Artist",
    )
    assert entry is None
    assert outcome == "single_file"


def test_artist_prefix_in_filename() -> None:
    torrent_bytes = _make_album_torrent(
        [
            ("KAROL G - 01 - Provenza.flac", 24_000_000),
            ("KAROL G - 02 - Tusa.flac", 23_000_000),
            ("KAROL G - 03 - Ocean (Remix).flac", 26_000_000),
        ],
        name="KAROL G - Ocean",
    )

    entry, outcome = select_track_file(
        torrent_bytes,
        wanted_title="Ocean (Remix)",
        artist="KAROL G",
    )

    assert outcome == "selected"
    assert entry is not None
    assert entry["file_index"] == 2
    assert entry["path"].endswith("KAROL G - 03 - Ocean (Remix).flac")


def test_fetch_torrent_bytes_handles_magnets_and_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    magnet_res = SimpleNamespace(
        title="Release",
        download_url="magnet:?xt=urn:btih:aabbcc",
    )
    assert fetch_torrent_bytes(magnet_res) is None

    http_res = SimpleNamespace(
        title="Secret Release",
        download_url="http://indexer.local/dl?passkey=supersecret123",
    )
    with patch(
        "trackseerr.clients.acquisition.torznab.fetch_torrent_file",
        return_value="magnet:?xt=urn:btih:redirected",
    ):
        assert fetch_torrent_bytes(http_res) is None

    with caplog.at_level(logging.INFO), patch(
        "trackseerr.clients.acquisition.torznab.fetch_torrent_file",
        side_effect=IndexerUnavailable("HTTP 503"),
    ):
        assert fetch_torrent_bytes(http_res) is None
        assert "supersecret123" not in caplog.text
