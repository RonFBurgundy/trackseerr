"""Shared predicate for OS/NAS system and trash folders that are never library or import content.

Dependency-free so the walkers, the scanner and the metadata clients can all import it without cycles.
"""

from __future__ import annotations

import re

# OS/NAS system and trash directories (lower-case; matched case-insensitively). Dot-names are listed explicitly:
# real artists start with dots (".38 Special", "...And You Will Know Us by the Trail of Dead").
SYSTEM_DIRNAMES = frozenset({
    "$recycle.bin", "recycler", "recycled", "system volume information", "lost+found", ".trash", ".trashes",
    "#recycle", "#snapshot", "@eadir", "@recycle", "@recently-snapshot", ".recycle", ".appledouble",
    ".spotlight-v100", ".fseventsd", ".temporaryitems", ".documentrevisions-v100", ".stfolder", ".stversions",
    ".sync", ".git",
})
SYSTEM_FILENAMES = frozenset({"thumbs.db", "desktop.ini", ".ds_store"})
_TRASH_N_RE = re.compile(r"^\.trash(-\d+)?$", re.IGNORECASE)
_WIN_SID_RE = re.compile(r"^S-1-5-21-[\d-]+$", re.IGNORECASE)
_WIN_STUB_RE = re.compile(r"^\$[IR][A-Z0-9]{6}$", re.IGNORECASE)


def is_system_dirname(name: str) -> bool:
    """True for an OS/NAS system or trash directory name (``$RECYCLE.BIN``, ``@eaDir``, ``.Trash-1000``...)."""
    low = name.lower()
    return low in SYSTEM_DIRNAMES or low.startswith(".trackseerr-") or bool(_TRASH_N_RE.match(low))


def is_system_filename(name: str) -> bool:
    """True for OS metadata files that are never library content (AppleDouble ``._*``, ``Thumbs.db``, ``.DS_Store``...).

    Files only: a directory named ``._x`` is not AppleDouble.
    """
    low = name.lower()
    return low in SYSTEM_FILENAMES or low.startswith("._")


def is_system_folder_name(name: str) -> bool:
    """True when an artist/album name came from a system/trash folder (or Windows recycle-bin artefact): never look it up."""
    stripped = (name or "").strip()
    return (
        not stripped
        or is_system_dirname(stripped)
        or bool(_WIN_SID_RE.match(stripped))
        or bool(_WIN_STUB_RE.match(stripped))
    )
