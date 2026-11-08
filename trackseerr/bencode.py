"""Strict bencode decoder and BitTorrent metadata parser."""

from __future__ import annotations

import hashlib
from typing import Any


class BencodeError(ValueError):
    """Raised when bencoded data is malformed or violates parser constraints."""


def _decode_bytes(data: bytes, idx: int) -> tuple[bytes, int]:
    colon = data.find(b":", idx)
    if colon == -1:
        raise BencodeError("String missing length colon separator")
    len_bytes = data[idx:colon]
    if not len_bytes.isdigit():
        raise BencodeError(f"Invalid string length digits: {len_bytes!r}")
    if len(len_bytes) > 1 and len_bytes.startswith(b"0"):
        raise BencodeError("Leading zero in string length")
    length = int(len_bytes)
    start = colon + 1
    end = start + length
    if end > len(data):
        raise BencodeError(
            f"Unexpected EOF: string requires {length} bytes but only {len(data) - start} available"
        )
    return data[start:end], end


def _decode_int(data: bytes, idx: int) -> tuple[int, int]:
    end = data.find(b"e", idx + 1)
    if end == -1:
        raise BencodeError("Unterminated integer")
    raw = data[idx + 1 : end]
    if not raw:
        raise BencodeError("Empty integer value")
    if raw == b"-0":
        raise BencodeError("Negative zero '-0' is invalid in bencode")
    if raw.startswith(b"-"):
        if len(raw) == 1:
            raise BencodeError("Invalid integer '-'")
        if raw[1:2] == b"0":
            raise BencodeError("Leading zero in negative integer")
    elif len(raw) > 1 and raw.startswith(b"0"):
        raise BencodeError("Leading zero in integer")
    try:
        val = int(raw.decode("ascii"))
    except ValueError as exc:
        raise BencodeError(f"Invalid integer content: {raw!r}") from exc
    return val, end + 1


def _decode_list(
    data: bytes,
    idx: int,
    depth: int,
    max_depth: int,
    info_span_box: list[tuple[int, int] | None] | None,
) -> tuple[list[Any], int]:
    idx += 1
    items = []
    while idx < len(data) and data[idx : idx + 1] != b"e":
        val, idx = _decode_value(data, idx, depth + 1, max_depth, info_span_box)
        items.append(val)
    if idx >= len(data) or data[idx : idx + 1] != b"e":
        raise BencodeError("Unterminated list")
    return items, idx + 1


def _decode_dict(
    data: bytes,
    idx: int,
    depth: int,
    max_depth: int,
    info_span_box: list[tuple[int, int] | None] | None,
) -> tuple[dict[bytes, Any], int]:
    idx += 1
    d: dict[bytes, Any] = {}
    prev_key: bytes | None = None
    while idx < len(data) and data[idx : idx + 1] != b"e":
        if not data[idx : idx + 1].isdigit():
            raise BencodeError("Dictionary key must be a bencoded string")
        key, idx = _decode_bytes(data, idx)
        if prev_key is not None and key == prev_key:
            raise BencodeError(f"Duplicate dictionary key: {key!r}")
        prev_key = key

        is_info_dict = (depth == 0 and key == b"info")
        val_start = idx
        val, idx = _decode_value(data, idx, depth + 1, max_depth, info_span_box)
        val_end = idx

        if is_info_dict and info_span_box is not None and info_span_box[0] is None:
            info_span_box[0] = (val_start, val_end)

        d[key] = val
    if idx >= len(data) or data[idx : idx + 1] != b"e":
        raise BencodeError("Unterminated dictionary")
    return d, idx + 1


def _decode_value(
    data: bytes,
    idx: int,
    depth: int,
    max_depth: int = 64,
    info_span_box: list[tuple[int, int] | None] | None = None,
) -> tuple[Any, int]:
    if depth > max_depth:
        raise BencodeError(f"Maximum recursion depth ({max_depth}) exceeded")
    if idx >= len(data):
        raise BencodeError("Unexpected EOF while parsing bencode")

    byte = data[idx : idx + 1]
    if byte == b"i":
        return _decode_int(data, idx)
    if byte == b"l":
        return _decode_list(data, idx, depth, max_depth, info_span_box)
    if byte == b"d":
        return _decode_dict(data, idx, depth, max_depth, info_span_box)
    if byte.isdigit():
        return _decode_bytes(data, idx)

    raise BencodeError(f"Unexpected token {byte!r} at index {idx}")


def decode(data: bytes, max_bytes: int = 10 * 1024 * 1024) -> Any:
    """Strict bencode decoder supporting ints, bytes, lists, and dicts.

    Rejects trailing data, recursion depth > 64, or inputs exceeding max_bytes.
    Raises BencodeError on malformed input.
    """
    if len(data) > max_bytes:
        raise BencodeError(f"Input exceeds maximum allowed size ({len(data)} > {max_bytes} bytes)")
    if not data:
        raise BencodeError("Cannot decode empty data")

    val, next_idx = _decode_value(data, 0, 0, max_depth=64)
    if next_idx < len(data):
        raise BencodeError(f"Trailing data after bencoded value at byte index {next_idx}")
    return val


def info_hash(torrent: bytes, max_bytes: int = 10 * 1024 * 1024) -> str:
    """Computes the SHA-1 hex digest of the raw bencoded 'info' dict byte slice.

    Locates the exact byte span while decoding rather than re-encoding.
    """
    if len(torrent) > max_bytes:
        raise BencodeError(f"Input exceeds maximum allowed size ({len(torrent)} > {max_bytes} bytes)")
    if not torrent:
        raise BencodeError("Cannot decode empty data")

    info_span_box: list[tuple[int, int] | None] = [None]
    val, next_idx = _decode_value(torrent, 0, 0, max_depth=64, info_span_box=info_span_box)
    if next_idx < len(torrent):
        raise BencodeError(f"Trailing data after bencoded value at byte index {next_idx}")

    span = info_span_box[0]
    if span is None:
        raise BencodeError("Torrent missing 'info' dictionary")

    raw_info = torrent[span[0] : span[1]]
    return hashlib.sha1(raw_info).hexdigest()


def _get_val(d: dict[Any, Any], *names: str) -> Any:
    for name in names:
        if name in d:
            return d[name]
        bname = name.encode("utf-8")
        if bname in d:
            return d[bname]
    return None


def torrent_files(meta: dict[Any, Any]) -> list[dict[str, Any]]:
    """Returns {file_index, path, size_bytes} in torrent order.

    Supports both single-file (info.name / info.length) and multi-file
    (info.files[].path joined under info.name) torrents. Decodes path parts
    as UTF-8 with errors='replace', preferring 'path.utf-8' and 'name.utf-8'
    when present. BEP-47 padding files (attr contains 'p') are omitted from the
    result, but file_index continues to count them so indices match qBittorrent.
    """
    info = _get_val(meta, "info")
    if not isinstance(info, dict):
        raise BencodeError("Torrent metadata missing 'info' dictionary")

    files = _get_val(info, "files")
    root_name_raw = _get_val(info, "name.utf-8", "name") or ""
    if isinstance(root_name_raw, bytes):
        root_name = root_name_raw.decode("utf-8", errors="replace")
    else:
        root_name = str(root_name_raw)

    results: list[dict[str, Any]] = []

    # Multi-file torrent
    if isinstance(files, list):
        for file_idx, file_dict in enumerate(files):
            if not isinstance(file_dict, dict):
                continue

            attr_raw = _get_val(file_dict, "attr")
            if attr_raw is not None:
                attr_str = (
                    attr_raw.decode("latin1", errors="ignore")
                    if isinstance(attr_raw, bytes)
                    else str(attr_raw)
                )
                if "p" in attr_str:
                    # BEP-47 padding file: ignore, but file_idx counts it
                    continue

            path_raw = _get_val(file_dict, "path.utf-8", "path")
            parts: list[str] = []
            if isinstance(path_raw, (list, tuple)):
                for p in path_raw:
                    if isinstance(p, bytes):
                        parts.append(p.decode("utf-8", errors="replace"))
                    else:
                        parts.append(str(p))
            elif isinstance(path_raw, bytes):
                parts.append(path_raw.decode("utf-8", errors="replace"))
            elif path_raw:
                parts.append(str(path_raw))

            subpath = "/".join(parts)
            full_path = f"{root_name}/{subpath}" if (root_name and subpath) else (root_name or subpath)

            length_val = _get_val(file_dict, "length")
            size_bytes = int(length_val) if length_val is not None else 0

            results.append({
                "file_index": file_idx,
                "path": full_path,
                "size_bytes": size_bytes,
            })
        return results

    # Single-file torrent
    attr_raw = _get_val(info, "attr")
    if attr_raw is not None:
        attr_str = (
            attr_raw.decode("latin1", errors="ignore")
            if isinstance(attr_raw, bytes)
            else str(attr_raw)
        )
        if "p" in attr_str:
            return []

    length_val = _get_val(info, "length")
    size_bytes = int(length_val) if length_val is not None else 0
    return [{
        "file_index": 0,
        "path": root_name,
        "size_bytes": size_bytes,
    }]


__all__ = [
    "BencodeError",
    "decode",
    "info_hash",
    "torrent_files",
]
