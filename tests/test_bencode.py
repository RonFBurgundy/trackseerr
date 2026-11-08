"""Tests for strict bencode decoder, info_hash calculation, and torrent_files parser."""

import hashlib
import pytest

from trackseerr.bencode import BencodeError, decode, info_hash, torrent_files


def test_decode_integers():
    assert decode(b"i0e") == 0
    assert decode(b"i42e") == 42
    assert decode(b"i-42e") == -42
    assert decode(b"i1234567890123456789e") == 1234567890123456789

    # Invalid integers
    with pytest.raises(BencodeError):
        decode(b"i-0e")
    with pytest.raises(BencodeError):
        decode(b"i03e")
    with pytest.raises(BencodeError):
        decode(b"i-03e")
    with pytest.raises(BencodeError):
        decode(b"ie")
    with pytest.raises(BencodeError):
        decode(b"i-e")
    with pytest.raises(BencodeError):
        decode(b"i123")
    with pytest.raises(BencodeError):
        decode(b"iabce")


def test_decode_byte_strings():
    assert decode(b"0:") == b""
    assert decode(b"4:spam") == b"spam"
    assert decode(b"10:0123456789") == b"0123456789"

    # Invalid byte strings
    with pytest.raises(BencodeError):
        decode(b"04:spam")
    with pytest.raises(BencodeError):
        decode(b"10:short")
    with pytest.raises(BencodeError):
        decode(b"spam")


def test_decode_lists():
    assert decode(b"le") == []
    assert decode(b"l4:spami42ee") == [b"spam", 42]
    assert decode(b"lli1ei2eeli3ei4eee") == [[1, 2], [3, 4]]

    with pytest.raises(BencodeError):
        decode(b"l4:spam")


def test_decode_dicts():
    assert decode(b"de") == {}
    assert decode(b"d4:spami42ee") == {b"spam": 42}
    assert decode(b"d3:bar4:spam3:fooi42ee") == {b"bar": b"spam", b"foo": 42}

    # Duplicate keys
    with pytest.raises(BencodeError):
        decode(b"d3:foo1:a3:foo1:be")

    # Non-string key
    with pytest.raises(BencodeError):
        decode(b"di1e4:spame")

    # Unterminated dict
    with pytest.raises(BencodeError):
        decode(b"d3:foo4:bar")


def test_decode_limits_and_errors():
    # Empty data
    with pytest.raises(BencodeError):
        decode(b"")

    # Oversize input
    with pytest.raises(BencodeError, match="exceeds maximum"):
        decode(b"i123e", max_bytes=4)

    # Trailing data
    with pytest.raises(BencodeError, match="Trailing data"):
        decode(b"i42eextra")

    with pytest.raises(BencodeError, match="Trailing data"):
        decode(b"4:spamtrailing")

    # Max recursion depth (64)
    valid_nesting = b"l" * 64 + b"i1e" + b"e" * 64
    assert decode(valid_nesting) is not None

    too_deep = b"l" * 65 + b"i1e" + b"e" * 65
    with pytest.raises(BencodeError, match="recursion depth"):
        decode(too_deep)


def test_torrent_files_single_file():
    meta = {
        b"info": {
            b"name": b"Single_Track.mp3",
            b"length": 5242880,
        }
    }
    files = torrent_files(meta)
    assert len(files) == 1
    assert files[0] == {
        "file_index": 0,
        "path": "Single_Track.mp3",
        "size_bytes": 5242880,
    }


def test_torrent_files_multi_file():
    meta = {
        b"info": {
            b"name": b"Artist - Album (2024)",
            b"files": [
                {
                    b"length": 1048576,
                    b"path": [b"CD1", b"01 - Track 1.flac"],
                },
                {
                    b"length": 2097152,
                    b"path": [b"CD1", b"02 - Track 2.flac"],
                },
            ],
        }
    }
    files = torrent_files(meta)
    assert len(files) == 2
    assert files[0] == {
        "file_index": 0,
        "path": "Artist - Album (2024)/CD1/01 - Track 1.flac",
        "size_bytes": 1048576,
    }
    assert files[1] == {
        "file_index": 1,
        "path": "Artist - Album (2024)/CD1/02 - Track 2.flac",
        "size_bytes": 2097152,
    }


def test_torrent_files_padding_files_keep_indices():
    meta = {
        b"info": {
            b"name": b"Album",
            b"files": [
                {
                    b"length": 1000,
                    b"path": [b"01.mp3"],
                },
                {
                    b"length": 24,
                    b"path": [b".pad", b"24"],
                    b"attr": b"p",  # BEP-47 padding file
                },
                {
                    b"length": 2000,
                    b"path": [b"02.mp3"],
                },
                {
                    b"length": 48,
                    b"path": [b".pad", b"48"],
                    b"attr": "p",  # string attr
                },
                {
                    b"length": 3000,
                    b"path": [b"03.mp3"],
                },
            ],
        }
    }
    files = torrent_files(meta)
    assert len(files) == 3
    # Padding files at index 1 and 3 are omitted, but indices match original list
    assert files[0]["file_index"] == 0
    assert files[0]["path"] == "Album/01.mp3"
    assert files[1]["file_index"] == 2
    assert files[1]["path"] == "Album/02.mp3"
    assert files[2]["file_index"] == 4
    assert files[2]["path"] == "Album/03.mp3"


def test_torrent_files_utf8_preference():
    meta = {
        b"info": {
            b"name": b"Old_ASCII_Name",
            b"name.utf-8": "Unicode Album (Édition)".encode("utf-8"),
            b"files": [
                {
                    b"length": 1000,
                    b"path": [b"01_old.mp3"],
                    b"path.utf-8": ["01 - Pièce No 1.flac".encode("utf-8")],
                }
            ],
        }
    }
    files = torrent_files(meta)
    assert len(files) == 1
    assert files[0]["path"] == "Unicode Album (Édition)/01 - Pièce No 1.flac"


def test_info_hash_matches_hashlib_reference():
    raw_info = b"d6:lengthi12345e4:name8:test.mp312:piece lengthi16384e6:pieces20:12345678901234567890e"
    expected_hash = hashlib.sha1(raw_info).hexdigest()

    torrent = (
        b"d8:announce16:http://tracker/a"
        b"7:comment11:TrackSeerr!"
        b"4:info" + raw_info +
        b"e"
    )

    computed_hash = info_hash(torrent)
    assert computed_hash == expected_hash

    # Missing info dictionary
    no_info_torrent = b"d8:announce16:http://tracker/ae"
    with pytest.raises(BencodeError, match="missing 'info'"):
        info_hash(no_info_torrent)
