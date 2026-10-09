"""Tests for Lidarr-style naming formats: optional blocks, full-path track formats, presets, previews."""

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.naming import (
    PRESETS,
    build_track_path,
    legacy_to_track_formats,
    render_template,
    split_template_segments,
    validate_format,
)
from trackseerr.storage import Database

META = {
    "artist": "The Beatles",
    "album": "The White Album",
    "title": "Revolution 1",
    "album_type": "Album",
    "release_year": 1968,
    "track_number": 1,
    "disc_number": 2,
    "total_discs": 2,
    "extension": ".flac",
}


def settings_for(preset: str, **extra):
    s = dict(PRESETS[preset])
    s["root_folder_path"] = "/music"
    s.update(extra)
    return s


class TestOptionalBlocks:
    def test_nested_block_keeps_brackets(self):
        assert render_template("{Album Title}{ - [{Album Type}]}", META) == "The White Album - [Album]"

    def test_bare_block_keeps_literal_parens(self):
        assert render_template("{Album Title}{ (Release Year)}", META) == "The White Album (1968)"

    def test_blocks_vanish_when_token_missing(self):
        meta = {k: v for k, v in META.items() if k not in ("album_type", "release_year")}
        assert render_template("{Album Title}{ - [{Album Type}]}{ (Release Year)}", meta) == "The White Album"

    def test_bracketed_bare_block(self):
        meta = dict(META, album_disambiguation="Remastered")
        assert render_template("{Album Title}{ [Album Disambiguation]}", meta) == "The White Album [Remastered]"
        assert render_template("{Album Title}{ [Album Disambiguation]}", META) == "The White Album"

    def test_leading_space_block_is_not_a_plain_token(self):
        assert render_template("{Album Title}{ Medium Format}", dict(META, medium_format="Vinyl")) == (
            "The White Album Vinyl"
        )

    def test_legacy_forms_unchanged(self):
        assert render_template("{Album Title}{[ - Album Type]}", META) == "The White Album - Album"
        assert render_template("{Album Title} {(Release Year)}", META) == "The White Album (1968)"


class TestSegments:
    def test_split_ignores_slashes_inside_blocks_and_trailing_slash(self):
        assert split_template_segments("{A}{ / [{B}]}/Disc {medium:00}/{track:00}/") == [
            "{A}{ / [{B}]}",
            "Disc {medium:00}",
            "{track:00}",
        ]

    def test_slash_in_token_value_does_not_split(self):
        meta = dict(META, artist="AC/DC", total_discs=1, disc_number=1)
        path = build_track_path(meta, settings_for("Trackseerr"))
        assert path.startswith("/music/AC-DC/")


class TestPresets:
    def test_trackseerr_standard(self):
        meta = dict(META, total_discs=1, disc_number=1)
        assert build_track_path(meta, settings_for("Trackseerr")) == (
            "/music/Beatles/The White Album - [Album] (1968)/01 - Revolution 1.flac"
        )

    def test_trackseerr_multi_disc(self):
        assert build_track_path(META, settings_for("Trackseerr")) == (
            "/music/Beatles/The White Album - [Album] (1968)/Disc 02/01 - Revolution 1.flac"
        )

    def test_trackseerr_without_type_or_year(self):
        meta = {k: v for k, v in META.items() if k not in ("album_type", "release_year")}
        assert build_track_path(meta, settings_for("Trackseerr")) == (
            "/music/Beatles/The White Album/Disc 02/01 - Revolution 1.flac"
        )

    def test_plex_multi_disc_uses_prefixed_track_numbers(self):
        assert build_track_path(META, settings_for("Plex")) == (
            "/music/The Beatles/The White Album/201 - Revolution 1.flac"
        )

    def test_trash_includes_artist_and_album_in_filename(self):
        meta = dict(META, total_discs=1, disc_number=1)
        assert build_track_path(meta, settings_for("TRaSH Guides")) == (
            "/music/The Beatles/The White Album (1968)/The Beatles - The White Album - 01 - Revolution 1.flac"
        )

    @pytest.mark.parametrize("name", list(PRESETS))
    def test_every_preset_renders_valid_paths(self, name):
        for meta in (META, dict(META, total_discs=1, disc_number=1)):
            path = build_track_path(meta, settings_for(name))
            assert path.startswith("/music/") and path.endswith(".flac")
            assert ".." not in path and "//" not in path.replace("/music/", "")

    def test_legacy_presets_match_legacy_composition(self):
        std, multi = legacy_to_track_formats(
            {
                "album_folder_format": "{Album Title}",
                "standard_track_format": "{track:00} - {Track Title}",
                "multi_disc_folder_format": "Disc {medium:0}",
            }
        )
        assert std == "{Album Title}/{track:00} - {Track Title}"
        assert multi == "{Album Title}/Disc {medium:0}/{track:00} - {Track Title}"
        assert PRESETS["Clean Minimal"]["multi_disc_track_format"] == (
            "{Album CleanTitle} ({Release Year})/Disc {medium:0}/{track:00} - {Track CleanTitle}"
        )

    def test_trailing_slash_in_multi_disc_format_is_harmless(self):
        s = settings_for(
            "Trackseerr",
            multi_disc_track_format=PRESETS["Trackseerr"]["multi_disc_track_format"] + "/",
        )
        assert build_track_path(META, s).endswith("/Disc 02/01 - Revolution 1.flac")


class TestValidation:
    def test_clean_formats_have_no_warnings(self):
        for preset in ("Trackseerr", "TRaSH Guides", "Plex"):
            p = PRESETS[preset]
            assert validate_format(p["standard_track_format"]) == []
            assert validate_format(p["multi_disc_track_format"]) == []
            assert validate_format(p["artist_folder_format"], "artist") == []

    def test_unknown_token_unbalanced_and_missing_track_token(self):
        assert any("Unknown token" in w for w in validate_format("{Album Titel}/{track:00} - {Track Title}"))
        assert any("Unbalanced" in w for w in validate_format("{Album Title/{track:00}"))
        assert any("overwrite" in w for w in validate_format("{Album Title}/Song"))
        assert any("single folder" in w for w in validate_format("{Artist Name}/x", "artist"))
        assert validate_format("") != []


def _headers(user, db, config):
    secret = get_or_create_secret_key(config.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


class TestPreviewApi:
    @pytest.fixture
    def env(self, tmp_path):
        db = Database(":memory:")
        config = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
        app = create_app(db=db, config=config)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_config] = lambda: config
        admin = db.upsert_user("admin-1", "admin_user", "a@plex.tv", is_admin=True)
        yield TestClient(app), _headers(admin, db, config), db
        db.close()

    def test_get_returns_new_presets_and_descriptions(self, env):
        client, headers, _ = env
        data = client.get("/api/settings/media-management", headers=headers).json()
        assert {"Trackseerr", "TRaSH Guides", "Plex"} <= set(data["presets"])
        assert data["preset_descriptions"]["Plex"]
        assert "multi_disc_track_format" in data["settings"]

    def test_migrated_defaults_are_full_paths(self, env):
        _, _, db = env
        s = db.get_media_management_settings()
        assert "/" in s["standard_track_format"] and s["multi_disc_track_format"]

    def test_preview_returns_per_format_samples_with_overrides(self, env):
        client, headers, _ = env
        body = {k: PRESETS["Trackseerr"][k] for k in ("artist_folder_format", "standard_track_format", "multi_disc_track_format")}
        data = client.post("/api/settings/media-management/preview", json=body, headers=headers).json()
        fp = data["format_previews"]
        assert set(fp) == {"artist_folder_format", "standard_track_format", "multi_disc_track_format"}
        assert fp["artist_folder_format"]["format"] == "{Artist CleanName}"
        by_id = {s["sample_id"]: s["output"] for s in fp["artist_folder_format"]["samples"]}
        assert by_id["multi_disc"] == "Beatles"
        typed = {s["sample_id"]: s["output"] for s in fp["standard_track_format"]["samples"]}
        assert typed["typed_release"] == "Twoism - [EP] (1995)/03 - Basefree.flac"
        multi = {s["sample_id"]: s["output"] for s in fp["multi_disc_track_format"]["samples"]}
        assert multi["standard"].endswith("/Disc 01/01 - Speak to Me.flac")
        assert all(f["warnings"] == [] for f in fp.values())

    def test_preview_surfaces_warnings(self, env):
        client, headers, _ = env
        data = client.post(
            "/api/settings/media-management/preview",
            json={"standard_track_format": "{Album Titel}/{track:00}"},
            headers=headers,
        ).json()
        assert any("Unknown token" in w for w in data["format_previews"]["standard_track_format"]["warnings"])

    def test_save_persists_multi_disc_track_format(self, env):
        client, headers, db = env
        resp = client.post(
            "/api/settings/media-management",
            json={"multi_disc_track_format": "{Album Title}/Disc {medium:00}/{track:00} - {Track Title}"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert db.get_media_management_settings()["multi_disc_track_format"].startswith("{Album Title}/Disc")


class TestArtistTokens:
    def test_the_variants_and_first_character(self):
        meta = dict(META, artist_genre="Pop", artist_mbid="db92a151-1ac2-438b-bc43-b82e149ddd50")
        assert render_template("{Artist NameThe}", meta) == "Beatles, The"
        assert render_template("{Artist CleanNameThe}", meta) == "Beatles, The"
        assert render_template("{Artist NameFirstCharacter}", meta) == "B"
        assert render_template("{Artist Genre}", meta) == "Pop"
        assert render_template("{Artist MbId}", meta) == "db92a151-1ac2-438b-bc43-b82e149ddd50"
        assert render_template("{Artist NameThe}", dict(META, artist="Queen")) == "Queen"

    def test_genre_and_mbid_optional_blocks_vanish_when_missing(self):
        assert render_template("{Artist Name}{ [Artist Genre]}", META) == "The Beatles"

    def test_every_token_is_documented(self):
        from trackseerr.naming import SUPPORTED_TOKENS, TOKEN_HELP

        documented = {t.strip("{}") for g in TOKEN_HELP for t, _, _ in g["tokens"]}
        assert set(SUPPORTED_TOKENS) <= documented
        for g in TOKEN_HELP:
            for t, _, _ in g["tokens"]:
                assert t.strip("{}") in SUPPORTED_TOKENS


class TestExtraTokens:
    def test_album_track_and_original_tokens(self):
        meta = dict(META, album_genre="Rock", album_mbid="abc", track_artist="The Who", file_path="/dl/01 rev.flac")
        meta["title"] = "The Fool on the Hill"
        assert render_template("{Album TitleThe}", meta) == "White Album, The"
        assert render_template("{Album CleanTitleThe}", meta) == "White Album, The"
        assert render_template("{Album TitleFirstCharacter}", meta) == "W"
        assert render_template("{Album Genre}/{Album MbId}", meta) == "Rock/abc"
        assert render_template("{Track TitleThe}", meta) == "Fool on the Hill, The"
        assert render_template("{Track ArtistName}|{Track ArtistCleanName}|{Track ArtistNameThe}", meta) == (
            "The Who|Who|Who, The"
        )
        assert render_template("{Original Filename}", meta) == "01 rev"
        assert render_template("{Track ArtistName}", META) == "The Beatles"  # falls back to artist
