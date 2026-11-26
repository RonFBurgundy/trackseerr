"""Fernet-based encryption helper for per-user media-server credentials."""

from __future__ import annotations

import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from trackseerr.security import safe_data_path

logger = logging.getLogger(__name__)

_KEY_FILENAME = ".server_credentials_key"
_cache_lock = threading.Lock()
_fernet_cache: dict[str, tuple[tuple[int, int, int], Fernet]] = {}


def _stat_sig(path: Path) -> tuple[int, int, int]:
    st = path.stat()
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _load_or_create_key(data_dir: str) -> tuple[tuple[int, int, int], bytes]:
    base = Path(data_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    if not os.access(base, os.W_OK):
        uid = os.getuid() if hasattr(os, "getuid") else "N/A"
        gid = os.getgid() if hasattr(os, "getgid") else "N/A"
        raise PermissionError(
            f"Data directory '{base}' is not writable (UID {uid}, GID {gid}). "
            "Cannot create or access server credentials key."
        )

    key_path = safe_data_path(_KEY_FILENAME, base_dir=str(base))
    if key_path.exists():
        try:
            key = key_path.read_bytes().strip()
            Fernet(key)
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass
            return _stat_sig(key_path), key
        except (OSError, ValueError) as exc:
            logger.warning("Could not read existing server credentials key at %s: %s", key_path, type(exc).__name__)

    new_key = Fernet.generate_key()
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(key_path), flags, 0o600)
    try:
        with open(fd, "wb") as fh:
            fh.write(new_key)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return _stat_sig(key_path), new_key


def _get_fernet(data_dir: str) -> Fernet:
    base_key = str(Path(data_dir).resolve())
    with _cache_lock:
        cached = _fernet_cache.get(base_key)
        if cached is not None:
            sig, fernet = cached
            try:
                key_path = safe_data_path(_KEY_FILENAME, base_dir=base_key)
                if _stat_sig(key_path) == sig:
                    return fernet
            except OSError:
                pass
        sig, key = _load_or_create_key(base_key)
        fernet = Fernet(key)
        _fernet_cache[base_key] = (sig, fernet)
        return fernet


def seal(plaintext: str, data_dir: str = "/data") -> str:
    """Encrypt ``plaintext`` with the data directory's Fernet key."""
    return _get_fernet(data_dir).encrypt(plaintext.encode("utf-8")).decode("ascii")


def unseal(token: str, data_dir: str = "/data") -> Optional[str]:
    """Decrypt ``token`` with the data directory's Fernet key; return None on invalid/rotated key."""
    if not token:
        return None
    try:
        return _get_fernet(data_dir).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        logger.warning("Failed to unseal stored media server credential (key missing or rotated)")
        return None


def migrate_key(from_dir: str, to_dir: str) -> None:
    """Move ``.server_credentials_key`` from ``from_dir`` to ``to_dir`` once if the target has none."""
    from_base = Path(from_dir).resolve()
    to_base = Path(to_dir).resolve()
    if from_base == to_base:
        return

    src = safe_data_path(_KEY_FILENAME, base_dir=str(from_base))
    if not src.exists():
        return

    to_base.mkdir(parents=True, exist_ok=True)
    dst = safe_data_path(_KEY_FILENAME, base_dir=str(to_base))
    if dst.exists():
        return

    shutil.move(str(src), str(dst))
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass
    logger.info("Migrated server credentials key from %s to %s", from_base, to_base)
