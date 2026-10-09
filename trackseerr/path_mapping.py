"""Server-path to local-path mapping for the library-health check. Pure: no I/O beyond what callers pass in.

A media server and Trackseerr usually mount the same library at different roots (``/data/music`` vs ``/music``).
Matching is per path component in NFC + casefold space, identical to the iTunes importer, whose helpers are reused.
"""

import unicodedata
from typing import Optional, Sequence

from trackseerr.itunes_import import apply_mappings, suggest_mappings

_SAMPLE = 2000
_MIN_COVERAGE = 0.5
# Widening stops at these: coverage cannot tell ``/data/music`` from ``/data``, but the library root is the former.
_ROOT_NAMES = frozenset({"music", "audio", "media", "library"})


def normalize_key(path: str) -> str:
    """NFC + casefold + forward slashes: the canonical form for set membership between server and disk paths."""
    return unicodedata.normalize("NFC", path.replace("\\", "/")).casefold()


def apply_mapping(
    server_path: str,
    mapping: Optional[tuple[str, str]],
    *,
    relative: bool,
    music_root: str,
) -> str:
    """Translate a server path to a local one.

    Relative paths (Subsonic) are joined onto ``music_root``. Absolute paths swap ``mapping``'s server prefix for its
    local prefix, component-wise (``/data/music2`` is not under ``/data/music``). With no mapping, or a path outside
    the mapped prefix, an absolute path is returned unchanged.
    """
    path = server_path.replace("\\", "/")
    if relative:
        root = music_root.replace("\\", "/").rstrip("/")
        return f"{root}/{path.lstrip('/')}"
    if not mapping:
        return server_path
    src, dst = mapping
    mapped = apply_mappings(path, [{"from": src, "to": dst}])
    return server_path if mapped is None else mapped


def _coverage(sample: Sequence[str], mapping: tuple[str, str], local_keys: set[str]) -> int:
    return sum(
        1 for p in sample if normalize_key(apply_mapping(p, mapping, relative=False, music_root="")) in local_keys
    )


def suggest_mapping(server_paths: Sequence[str], local_paths: Sequence[str]) -> Optional[tuple[str, str]]:
    """Best ``(server_prefix, local_prefix)`` pair, or None when under half the server sample lands on a local file."""
    if not server_paths or not local_paths:
        return None
    step = max(1, len(server_paths) // _SAMPLE)
    sample = list(server_paths[::step][:_SAMPLE])
    local_keys = {normalize_key(p) for p in local_paths}
    best: Optional[tuple[str, str]] = None
    best_hits = 0
    for cand in suggest_mappings(list(server_paths), list(local_paths)):
        pair = (str(cand["from"]), str(cand["to"]))
        hits = _coverage(sample, pair, local_keys)
        if hits > best_hits:
            best, best_hits = pair, hits
    if best is None or best_hits < len(sample) * _MIN_COVERAGE:
        return None
    return _widen_to_root(best, best_hits, sample, local_keys)


def _widen_to_root(
    pair: tuple[str, str], hits: int, sample: Sequence[str], local_keys: set[str]
) -> tuple[str, str]:
    """Drops trailing components both prefixes share (``/data/music/Art`` + ``/music/Art`` -> ``/data/music`` +
    ``/music``) while coverage on the sample does not fall: the library-root pair, not the deepest common prefix."""
    src, dst = pair
    while True:
        s_parts = src.replace("\\", "/").rstrip("/").rsplit("/", 1)
        d_parts = dst.replace("\\", "/").rstrip("/").rsplit("/", 1)
        if len(s_parts) < 2 or len(d_parts) < 2 or not s_parts[0] or not d_parts[0]:
            return src, dst
        if normalize_key(s_parts[1]) != normalize_key(d_parts[1]) or normalize_key(s_parts[1]) in _ROOT_NAMES:
            return src, dst
        wider = (s_parts[0], d_parts[0])
        if _coverage(sample, wider, local_keys) < hits:
            return src, dst
        src, dst = wider
