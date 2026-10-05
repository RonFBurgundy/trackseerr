"""Validation and guarded evaluation of user-supplied regular expressions.

The decision engine evaluates admin-authored (or imported Lidarr/TRaSH) regexes against untrusted indexer titles.
Neither the ``regex`` module (with its per-call timeout) nor ``rapidfuzz`` is a dependency, so protection is layered
on the standard ``re`` module:

1. **Validation on save** (``validate_pattern``): length cap (500), must compile, and catastrophic-backtracking
   shapes are rejected by walking the parsed pattern tree: a variable-width repeat nested inside another
   variable-width repeat (``(a+)+``, ``(\\w*\\s?)*``), and a variable repeat over alternatives that can begin with the
   same character (``(a|aa)+``).
2. **Bounded input**: titles are truncated to ``MAX_INPUT_LEN`` characters before matching, which caps the cost of
   any pattern that slips past validation to a small polynomial.
3. **Per-title time budget** (``Budget``): the engine checks the clock between regex evaluations and skips the
   remaining regexes once the budget is spent, flagging it in the breakdown. This is cooperative: it cannot
   interrupt a single ``re.search`` already running, which is why (1) and (2) exist.

.NET named groups (``(?<name>...)``), as found in Lidarr/Servarr JSON, are translated to Python syntax.
"""

from __future__ import annotations

import functools
import re
import time
from typing import Optional

import re._constants as _sre_const  # type: ignore[import-not-found]
import re._parser as _sre_parse  # type: ignore[import-not-found]

MAX_PATTERN_LEN = 500
MAX_INPUT_LEN = 1024
DEFAULT_BUDGET_SECONDS = 0.25

_NAMED_GROUP = re.compile(r"\(\?<([A-Za-z_][A-Za-z0-9_]*)>")
_REPEAT_OPS = {_sre_const.MAX_REPEAT, _sre_const.MIN_REPEAT}
if hasattr(_sre_const, "POSSESSIVE_REPEAT"):
    _REPEAT_OPS.add(_sre_const.POSSESSIVE_REPEAT)
_MAXREPEAT = _sre_const.MAXREPEAT
_BIG_REPEAT = 10


class UnsafeRegexError(ValueError):
    """Raised when a pattern is too long, does not compile, or has a catastrophic-backtracking shape."""


def translate_pattern(pattern: str) -> str:
    """Converts .NET-only syntax to its Python equivalent (named groups)."""
    return _NAMED_GROUP.sub(r"(?P<\1>", pattern)


def _is_variable(lo: int, hi: int) -> bool:
    return hi != lo


def _first_chars(items: list) -> Optional[frozenset[str]]:
    """Set of characters a sub-pattern can start with, or None when it is open-ended (class, dot, repeat, ...)."""
    if not items:
        return frozenset()
    op, av = items[0]
    if op == _sre_const.LITERAL:
        return frozenset({chr(av).lower()})
    if op == _sre_const.SUBPATTERN:
        return _first_chars(list(av[-1]))
    if op == _sre_const.BRANCH:
        out: set[str] = set()
        for alt in av[1]:
            sub = _first_chars(list(alt))
            if sub is None:
                return None
            out |= sub
        return frozenset(out)
    return None


def _contains(items, predicate) -> bool:
    for op, av in items:
        if predicate(op, av):
            return True
        for child in _children(op, av):
            if _contains(child, predicate):
                return True
    return False


def _children(op, av) -> list[list]:
    if op in _REPEAT_OPS:
        return [list(av[2])]
    if op == _sre_const.SUBPATTERN:
        return [list(av[-1])]
    if op == _sre_const.BRANCH:
        return [list(alt) for alt in av[1]]
    if op in (_sre_const.ASSERT, _sre_const.ASSERT_NOT):
        return [list(av[1])]
    if hasattr(_sre_const, "ATOMIC_GROUP") and op == _sre_const.ATOMIC_GROUP:
        return [list(av)]
    if op == _sre_const.GROUPREF_EXISTS:
        return [list(av[1])] + ([list(av[2])] if av[2] else [])
    return []


def _check(items: list) -> None:
    for op, av in items:
        if op in _REPEAT_OPS:
            lo, hi, sub = av
            sub = list(sub)
            # Only repeats that can run long matter: an optional group (``?``) cannot multiply backtracking.
            if _is_variable(lo, hi) and hi > _BIG_REPEAT:
                if _contains(sub, lambda o, a: o in _REPEAT_OPS and _is_variable(a[0], a[1]) and a[1] > 1):
                    raise UnsafeRegexError("nested quantifier (a variable repeat inside a variable repeat)")

                def _overlapping_branch(o, a) -> bool:
                    if o != _sre_const.BRANCH:
                        return False
                    seen: set[str] = set()
                    for alt in a[1]:
                        first = _first_chars(list(alt))
                        if not list(alt):
                            return True  # an empty alternative (e.g. the factored form of ``a|aa``) overlaps
                        if first is None or (seen & first):
                            return True
                        seen |= first
                    return False

                if _contains(sub, _overlapping_branch):
                    raise UnsafeRegexError("repeated alternation with overlapping branches")
        for child in _children(op, av):
            _check(child)


@functools.lru_cache(maxsize=2048)
def _validated(pattern: str) -> re.Pattern[str]:
    if not isinstance(pattern, str) or not pattern:
        raise UnsafeRegexError("pattern is empty")
    if len(pattern) > MAX_PATTERN_LEN:
        raise UnsafeRegexError(f"pattern is longer than {MAX_PATTERN_LEN} characters")
    translated = translate_pattern(pattern)
    try:
        compiled = re.compile(translated, re.IGNORECASE)
        tree = _sre_parse.parse(translated, re.IGNORECASE)
    except (re.error, RecursionError, OverflowError) as exc:
        raise UnsafeRegexError(f"invalid regular expression: {exc}") from exc
    _check(list(tree))
    return compiled


def validate_pattern(pattern: str) -> None:
    """Raises ``UnsafeRegexError`` unless the pattern is acceptable (see module docstring)."""
    _validated(pattern)


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Returns the validated, case-insensitive compiled pattern (cached). Raises ``UnsafeRegexError``."""
    return _validated(pattern)


class Budget:
    """Cooperative per-title wall-clock budget for regex evaluation."""

    def __init__(self, seconds: float = DEFAULT_BUDGET_SECONDS) -> None:
        self._deadline = time.monotonic() + max(0.0, float(seconds))
        self.exhausted = False

    def spent(self) -> bool:
        if not self.exhausted and time.monotonic() > self._deadline:
            self.exhausted = True
        return self.exhausted


def safe_search(pattern: str, text: str, budget: Optional[Budget] = None) -> Optional[bool]:
    """Case-insensitive ``search``; True/False on a result, None when skipped (invalid pattern or budget spent)."""
    if budget is not None and budget.spent():
        return None
    try:
        compiled = compile_pattern(pattern)
    except UnsafeRegexError:
        return None
    return compiled.search((text or "")[:MAX_INPUT_LEN]) is not None
