"""Tests for trackseerr.artist_folders: observed_artist_folder and resolve_artist_folder."""

import sqlite3
from unittest.mock import MagicMock

from trackseerr.artist_folders import (
    _extract_artist_folder,
    observed_artist_folder,
    resolve_artist_folder,
)


def test_extract_artist_folder():
    norm_root = "/music"
    assert _extract_artist_folder("/music/Artist/Album/01.flac", norm_root) == "Artist"
    assert _extract_artist_folder("/music/Artist/01.flac", norm_root) == "Artist"
    assert _extract_artist_folder("/music/01.flac", norm_root) is None
    assert _extract_artist_folder("/outside/Artist/01.flac", norm_root) is None
    assert _extract_artist_folder("/music/Various Artists/01.flac", norm_root) is None
    assert _extract_artist_folder("/music/Various/01.flac", norm_root) is None
    assert _extract_artist_folder("", norm_root) is None


def test_observed_artist_folder_majority_and_tie_breaking():
    db = MagicMock()
    settings = {"root_folder_path": "/music", "rename_artist_folders": False}

    # No files
    db.list_artist_file_paths.return_value = []
    assert observed_artist_folder(db, "art-1", settings) is None

    # Majority wins
    db.list_artist_file_paths.return_value = [
        "/music/Crosby, Stills & Nash/Alb/01.flac",
        "/music/Crosby, Stills & Nash/Alb/02.flac",
        "/music/Crosby Stills and Nash/Alb/03.flac",
    ]
    assert observed_artist_folder(db, "art-1", settings) == "Crosby, Stills & Nash"

    # Ignores files outside root and Various Artists
    db.list_artist_file_paths.return_value = [
        "/outside/Folder1/01.flac",
        "/music/Various Artists/Comp/01.flac",
        "/music/Folder2/Alb/01.flac",
    ]
    assert observed_artist_folder(db, "art-1", settings) == "Folder2"

    # Tie broken by existing_artist_folder
    db.list_artist_file_paths.return_value = [
        "/music/Folder A/Alb/01.flac",
        "/music/Folder B/Alb/02.flac",
    ]
    artist = {"id": "art-1", "path": "/music/Folder B"}
    assert observed_artist_folder(db, "art-1", settings, artist=artist) == "Folder B"

    # Tie broken alphabetically if existing_artist_folder doesn't match either
    artist_other = {"id": "art-1", "path": "/music/Folder C"}
    assert observed_artist_folder(db, "art-1", settings, artist=artist_other) == "Folder A"


def test_resolve_artist_folder():
    db = MagicMock()
    settings = {"root_folder_path": "/music", "rename_artist_folders": False}
    artist = {"id": "art-1", "path": "/music/Existing Folder"}

    # rename_artist_folders=True returns None
    assert resolve_artist_folder(db, artist, {**settings, "rename_artist_folders": True}) is None

    # Artist with files uses observed folder
    db.list_artist_file_paths.return_value = ["/music/Observed Folder/Alb/01.flac"]
    assert resolve_artist_folder(db, artist, settings) == "Observed Folder"

    # Artist without files falls back to existing_artist_folder
    db.list_artist_file_paths.return_value = []
    assert resolve_artist_folder(db, artist, settings) == "Existing Folder"

    # sqlite3.Error falls back to existing_artist_folder
    db.list_artist_file_paths.side_effect = sqlite3.OperationalError("db locked")
    assert resolve_artist_folder(db, artist, settings) == "Existing Folder"

    # db=None falls back to existing_artist_folder
    assert resolve_artist_folder(None, artist, settings) == "Existing Folder"
