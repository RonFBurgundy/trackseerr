"""Comprehensive tests for Arr-grade Token Template Naming Engine and Path Sanitizer."""

import pytest

from trackseerr.naming import (
    build_track_path,
    format_quality,
    render_template,
    sanitize_component,
    strip_leading_articles,
)


class TestStripLeadingArticles:
    """Validates article stripping for clean artist and album names."""

    def test_strip_the(self):
        assert strip_leading_articles("The Beatles") == "Beatles"
        assert strip_leading_articles("the rolling stones") == "rolling stones"
        assert strip_leading_articles("The The") == "The"

    def test_strip_a(self):
        assert strip_leading_articles("A Perfect Circle") == "Perfect Circle"
        assert strip_leading_articles("a tribe called quest") == "tribe called quest"

    def test_strip_an(self):
        assert strip_leading_articles("An Autumn for Crippled Children") == "Autumn for Crippled Children"

    def test_preserve_names_without_articles(self):
        assert strip_leading_articles("Pink Floyd") == "Pink Floyd"
        assert strip_leading_articles("Theme Park") == "Theme Park"
        assert strip_leading_articles("Another Sky") == "Another Sky"
        assert strip_leading_articles("") == ""
        assert strip_leading_articles(None) == ""


class TestTokenReplacement:
    """Validates token replacement for all supported tokens."""

    def test_all_supported_tokens_resolve(self):
        metadata = {
            "artist": "The Beatles",
            "artist_disambiguation": "UK",
            "album": "The White Album",
            "album_disambiguation": "Deluxe",
            "release_year": "1968",
            "original_release_year": "1968",
            "album_type": "Album",
            "medium_format": "CD",
            "medium_title": "Disc 1",
            "disc_number": 1,
            "total_discs": 2,
            "track_number": 7,
            "title": "While My Guitar Gently Weeps",
            "codec": "FLAC",
            "bitrate": 1411000,
            "sample_rate": 44100,
            "bits_per_sample": 16,
            "quality_full": "FLAC 16bit 44.1kHz",
        }

        assert render_template("{Artist Name}", metadata) == "The Beatles"
        assert render_template("{Artist CleanName}", metadata) == "Beatles"
        assert render_template("{Artist Disambiguation}", metadata) == "UK"

        assert render_template("{Album Title}", metadata) == "The White Album"
        assert render_template("{Album CleanTitle}", metadata) == "White Album"
        assert render_template("{Release Year}", metadata) == "1968"
        assert render_template("{Original Release Year}", metadata) == "1968"
        assert render_template("{Album Disambiguation}", metadata) == "Deluxe"
        assert render_template("{Album Type}", metadata) == "Album"

        assert render_template("{medium:00}", metadata) == "01"
        assert render_template("{medium:0}", metadata) == "1"
        assert render_template("{disc:00}", metadata) == "01"
        assert render_template("{disc:0}", metadata) == "1"
        assert render_template("{Medium Format}", metadata) == "CD"
        assert render_template("{Medium Title}", metadata) == "Disc 1"

        assert render_template("{track:00}", metadata) == "07"
        assert render_template("{track:0}", metadata) == "7"
        assert render_template("{Track Title}", metadata) == "While My Guitar Gently Weeps"
        assert render_template("{Track CleanTitle}", metadata) == "While My Guitar Gently Weeps"

        assert render_template("{Quality Full}", metadata) == "FLAC 16bit 44.1kHz"
        assert render_template("{MediaInfo AudioCodec}", metadata) == "FLAC"
        assert render_template("{MediaInfo Bitrate}", metadata) == "1411kbps"
        assert render_template("{MediaInfo SampleRate}", metadata) == "44.1kHz"
        assert render_template("{MediaInfo BitDepth}", metadata) == "16bit"

    def test_clean_artist_names_tokens(self):
        metadata = {"artist": "The Beatles"}
        assert render_template("{Artist Name}", metadata) == "The Beatles"
        assert render_template("{Artist CleanName}", metadata) == "Beatles"

    def test_format_quality_generator(self):
        lossless = {
            "codec": "FLAC",
            "bits_per_sample": 24,
            "sample_rate": 96000,
        }
        assert format_quality(lossless) == "FLAC 24bit 96kHz"

        lossy = {
            "codec": "MP3",
            "bitrate": 320000,
            "sample_rate": 44100,
        }
        assert format_quality(lossy) == "MP3 320kbps"

        custom = {"quality_full": "Custom Audiophile Master"}
        assert format_quality(custom) == "Custom Audiophile Master"


class TestConditionalBlocks:
    """Validates {[prefix]Token[suffix]} and {(Token)} conditional evaluation."""

    def test_conditional_block_present(self):
        meta_with_type = {"album": "Kid A", "release_year": "2000", "album_type": "EP"}
        template = "{Album Title} ({Release Year}){[ - Album Type]}"
        rendered = render_template(template, meta_with_type)
        assert rendered == "Kid A (2000) - EP"

    def test_conditional_block_omitted_when_empty(self):
        meta_no_type = {"album": "Kid A", "release_year": "2000", "album_type": None}
        template = "{Album Title} ({Release Year}){[ - Album Type]}"
        rendered = render_template(template, meta_no_type)
        assert rendered == "Kid A (2000)"

    def test_quality_conditional_block(self):
        template = "{track:00} - {Track Title}{[ (Quality Full)]}"

        meta_with_q = {"track_number": 1, "title": "Airbag", "quality_full": "FLAC 24bit 96kHz"}
        assert render_template(template, meta_with_q) == "01 - Airbag (FLAC 24bit 96kHz)"

        meta_without_q = {"track_number": 1, "title": "Airbag", "quality_full": None, "codec": None}
        assert render_template(template, meta_without_q) == "01 - Airbag"

    def test_parentheses_conditional(self):
        template = "{Album Title} {([Disambiguation: ]Album Disambiguation)}"
        meta_dis = {"album": "In Rainbows", "album_disambiguation": "Disc 2"}
        assert render_template(template, meta_dis) == "In Rainbows (Disambiguation: Disc 2)"

        meta_no_dis = {"album": "In Rainbows", "album_disambiguation": None}
        assert render_template(template, meta_no_dis) == "In Rainbows"


class TestCrossPlatformSanitization:
    """Validates path and filename component scrubbing."""

    def test_colon_replacement(self):
        assert sanitize_component("Star Wars: Episode IV", colon_replacement=" - ") == "Star Wars - Episode IV"
        assert sanitize_component("Album: Name: Here", colon_replacement="_") == "Album_Name_Here"
        assert sanitize_component("Title: Subtitle", colon_replacement=" ") == "Title Subtitle"

    def test_illegal_character_stripping(self):
        raw = '<Invalid> "Path" | Name? *Test* /Directory\\ \x00\x1f'
        cleaned = sanitize_component(raw)
        assert cleaned == "Invalid Path Name Test -Directory"  # "/" separates words, so it becomes "-"

    def test_trailing_dot_and_space_elimination(self):
        # Trims trailing dots preventing SMB/CIFS errors while preserving internal dots
        assert sanitize_component("R.E.M.") == "R.E.M"
        assert sanitize_component("Artist Name.  ") == "Artist Name"
        assert sanitize_component("Album Title....") == "Album Title"
        assert sanitize_component("  Track 01 . ") == "Track 01"

    def test_255_byte_utf8_truncation(self):
        # ASCII long component
        long_ascii = "A" * 300
        truncated_ascii = sanitize_component(long_ascii)
        assert len(truncated_ascii.encode("utf-8")) == 255
        assert truncated_ascii == "A" * 255

        # 3-byte CJK glyph boundary truncation
        # 253 ASCII chars + 3-byte glyph ('世' is 3 bytes) -> 256 bytes total
        cjk_str = ("B" * 253) + "世界"
        truncated_cjk = sanitize_component(cjk_str)
        encoded_cjk = truncated_cjk.encode("utf-8")
        assert len(encoded_cjk) <= 255
        # Must decode cleanly without corrupting multi-byte code point
        assert truncated_cjk.encode("utf-8").decode("utf-8") == truncated_cjk

        # 4-byte Emoji boundary truncation
        emoji_str = ("E" * 253) + "🎵🎶"
        truncated_emoji = sanitize_component(emoji_str)
        assert len(truncated_emoji.encode("utf-8")) <= 255
        assert truncated_emoji.encode("utf-8").decode("utf-8") == truncated_emoji


class TestPathBuilder:
    """Validates track path generation with single vs multi-disc and compilation modes."""

    @pytest.fixture
    def default_settings(self):
        return {
            "root_folder_path": "/music",
            "artist_folder_format": "{Artist Name}",
            "album_folder_format": "{Album Title} ({Release Year}){[ - Album Type]}",
            "standard_track_format": "{track:00} - {Track Title}{[ (Quality Full)]}",
            "compilation_track_format": "{track:00} - {Artist Name} - {Track Title}{[ (Quality Full)]}",
            "multi_disc_folder_format": "{Medium Format} {medium:00}",
            "colon_replacement_format": " - ",
            "clean_artist_names": False,
        }

    def test_standard_single_disc_path(self, default_settings):
        metadata = {
            "artist": "Pink Floyd",
            "album": "The Dark Side of the Moon",
            "title": "Speak to Me",
            "release_year": "1973",
            "track_number": 1,
            "disc_number": 1,
            "total_discs": 1,
            "codec": "FLAC",
            "quality_full": "FLAC 24bit 96kHz",
            "extension": ".flac",
        }
        path = build_track_path(metadata, default_settings)
        assert path == "/music/Pink Floyd/The Dark Side of the Moon (1973)/01 - Speak to Me (FLAC 24bit 96kHz).flac"

    def test_multi_disc_omission_when_total_discs_is_one(self, default_settings):
        metadata = {
            "artist": "Nirvana",
            "album": "Nevermind",
            "title": "Smells Like Teen Spirit",
            "release_year": "1991",
            "track_number": 1,
            "disc_number": 1,
            "total_discs": 1,
            "codec": "MP3",
            "quality_full": "MP3 320kbps",
            "extension": ".mp3",
        }
        path = build_track_path(metadata, default_settings)
        assert "CD 01" not in path
        assert path == "/music/Nirvana/Nevermind (1991)/01 - Smells Like Teen Spirit (MP3 320kbps).mp3"

    def test_multi_disc_presence_when_disc_gt_one(self, default_settings):
        metadata = {
            "artist": "The Beatles",
            "album": "The Beatles (White Album)",
            "title": "Revolution 1",
            "release_year": "1968",
            "medium_format": "CD",
            "track_number": 1,
            "disc_number": 2,
            "total_discs": 2,
            "codec": "FLAC",
            "quality_full": "FLAC 16bit 44.1kHz",
            "extension": ".flac",
        }
        path = build_track_path(metadata, default_settings)
        assert "/CD 02/" in path
        assert path == "/music/The Beatles/The Beatles (White Album) (1968)/CD 02/01 - Revolution 1 (FLAC 16bit 44.1kHz).flac"

    def test_compilation_various_artists_path(self, default_settings):
        metadata = {
            "artist": "Queen",
            "album_artist": "Various Artists",
            "album": "Wayne's World: Music from the Motion Picture",
            "title": "Bohemian Rhapsody",
            "release_year": "1992",
            "album_type": "Soundtrack",
            "track_number": 1,
            "disc_number": 1,
            "total_discs": 1,
            "is_compilation": True,
            "codec": "MP3",
            "quality_full": "MP3 320kbps",
            "extension": ".mp3",
        }
        path = build_track_path(metadata, default_settings)
        assert path == "/music/Various Artists/Wayne's World - Music from the Motion Picture (1992) - Soundtrack/01 - Queen - Bohemian Rhapsody (MP3 320kbps).mp3"

    def test_directory_traversal_prevention(self, default_settings):
        malicious_meta = {
            "artist": "../../etc/shadow",
            "album": "../../../var/log",
            "title": "../../secret",
            "release_year": "2024",
            "track_number": 1,
            "extension": ".flac",
        }
        path = build_track_path(malicious_meta, default_settings)
        assert ".." not in path
        assert path.startswith("/music/")
        assert "etc/shadow" not in path
