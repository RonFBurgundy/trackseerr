"""Removal of empty download folders left behind by move-mode imports and slskd transfers.

Only empty directories are ever removed (``Path.rmdir``): never a file, never a non-empty directory.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


def _root_is_unsafe(root: Path) -> bool:
    """True when ``root`` is the filesystem root or too shallow to be a plausible download folder."""
    return root == Path(root.anchor) or len(root.parts) < 2


def _resolve_root(root: Path) -> Optional[Path]:
    try:
        resolved = Path(root).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        logger.debug("Could not resolve cleanup root %s: %s", root, exc)
        return None
    if _root_is_unsafe(resolved):
        logger.warning("Refusing to clean up empty folders under unsafe root %s", resolved)
        return None
    return resolved


def _strictly_under(path: Path, root: Path) -> bool:
    return root in path.parents


def prune_empty_parents(paths: Iterable[str | Path], root: Path) -> int:
    """Remove now-empty parent directories of ``paths``, walking up toward (never including) ``root``."""
    resolved_root = _resolve_root(root)
    if resolved_root is None:
        return 0
    start_dirs: list[Path] = []
    for raw in paths:
        try:
            parent = Path(raw).parent.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            logger.debug("Could not resolve %s for pruning: %s", raw, exc)
            continue
        if parent not in start_dirs:
            start_dirs.append(parent)
    removed = 0
    seen: set[Path] = set()
    for directory in start_dirs:
        current = directory
        while _strictly_under(current, resolved_root) and current not in seen:
            seen.add(current)
            try:
                current.rmdir()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.debug("Not removing %s: %s", current, exc)
                break
            else:
                removed += 1
            current = current.parent
    if removed:
        logger.info("Removed %d empty download folder(s) under %s", removed, resolved_root)
    return removed


def sweep_empty_dirs(root: Path, min_age_seconds: float, now: Optional[float] = None) -> int:
    """Remove empty directories under ``root`` whose mtime is older than ``min_age_seconds`` (bottom-up)."""
    resolved_root = _resolve_root(root)
    if resolved_root is None:
        return 0
    if not resolved_root.is_dir():
        logger.debug("Cleanup root %s is not an existing directory", resolved_root)
        return 0
    current_time = time.time() if now is None else now
    removed = 0
    # Removing a child bumps its parent's mtime, so ages are read before anything is deleted.
    mtimes: dict[Path, float] = {}
    walked: list[Path] = []
    for dirpath, _dirnames, _filenames in os.walk(resolved_root, topdown=False, followlinks=False):
        directory = Path(dirpath)
        if directory == resolved_root or directory.is_symlink():
            continue
        try:
            mtimes[directory] = directory.stat().st_mtime
        except OSError as exc:
            logger.debug("Could not stat %s: %s", directory, exc)
            continue
        walked.append(directory)
    for directory in walked:
        try:
            if current_time - mtimes[directory] <= min_age_seconds:
                continue
            directory.rmdir()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.debug("Not removing %s: %s", directory, exc)
            continue
        removed += 1
    if removed:
        logger.info("Swept %d empty folder(s) under %s", removed, resolved_root)
    return removed
