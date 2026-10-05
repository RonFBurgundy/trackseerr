"""Validation and guarded evaluation of user-supplied regular expressions.

The decision engine evaluates admin-authored (or imported Lidarr/TRaSH) regexes against untrusted indexer titles.
Python's ``re`` holds the GIL and cannot be interrupted, so protection is layered:

1. **Hard timeout** (the real guarantee): every user pattern is compiled and run with the third-party ``regex``
   module, whose ``search(..., timeout=)`` aborts a runaway match (``SEARCH_TIMEOUT_SECONDS``, 50 ms). A
   ``TimeoutError`` is surfaced as ``BudgetExceeded``.
2. **Validation on save** (``validate_pattern``): length cap (500), must parse, and catastrophic shapes are rejected
   by walking the parsed tree: a variable repeat nested in a variable repeat (``(a+)+``, ``(a+){2,10}b``), a
   repeat over alternatives that can begin with the same character (``(a|aa)+``), more than 3 variable-width
   repeats in one sequence (``.*.*.*.*x``), and backreferences under a repeat or to a group inside one
   (``(a|b)*\\1``).
3. **Bounded input**: titles are truncated to ``REGEX_INPUT_LEN`` (256) characters before matching.
4. **Per-title time budget** (``Budget``): cooperative wall-clock budget across all regexes of one evaluation.

Semantics: a spent budget or a timeout raises ``BudgetExceeded`` from ``safe_search``. The decision engine treats it
as a REJECT (code ``evaluation_budget_exceeded``) for required terms, ignored terms and format specs, so a verdict
never fails open. An invalid or unsafe stored pattern returns ``None`` (skipped, noted in the breakdown).

Validation parses with ``re``'s parser, so only syntax valid in both ``re`` and ``regex`` is accepted. .NET named
groups (``(?<name>...)``), as found in Lidarr/Servarr JSON, are translated to Python syntax.
"""

from __future__ import annotations

import functools
import re
import time
from typing import Optional

import regex as _regex

import re._constants as _sre_const  # type: ignore[import-not-found]
import re._parser as _sre_parse  # type: ignore[import-not-found]

MAX_PATTERN_LEN = 500
MAX_INPUT_LEN = 1024
REGEX_INPUT_LEN = 256
SEARCH_TIMEOUT_SECONDS = 0.05
MAX_VARIABLE_REPEATS = 3
DEFAULT_BUDGET_SECONDS = 0.25

_NAMED_GROUP = re.compile(r"\(\?<([A-Za-z_][A-Za-z0-9_]*)>")
_REPEAT_OPS = {_sre_const.MAX_REPEAT, _sre_const.MIN_REPEAT}
if hasattr(_sre_const, "POSSESSIVE_REPEAT"):
    _REPEAT_OPS.add(_sre_const.POSSESSIVE_REPEAT)
_MAXREPEAT = _sre_const.MAXREPEAT
_BIG_REPEAT = 10


class UnsafeRegexError(ValueError):
    """Raised when a pattern is too long, does not compile, or has a catastrophic-backtracking shape."""


class BudgetExceeded(Exception):
    """The per-title budget is spent or a single search hit its timeout; the verdict must reject, not skip."""


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


def _check(items: list) -> None:
    variable = 0
    for op, av in items:
        if op in _REPEAT_OPS:
            lo, hi, sub = av
            sub = list(sub)
            if _is_variable(lo, hi):
                variable += 1
                # A ``?`` (hi == 1) cannot multiply backtracking; any longer variable repeat can, whatever its bound.
                if hi > 1:
                    if _contains(sub, lambda o, a: o in _REPEAT_OPS and _is_variable(a[0], a[1]) and a[1] > 1):
                        raise UnsafeRegexError("nested quantifier (a variable repeat inside a variable repeat)")
                    if _contains(sub, _overlapping_branch):
                        raise UnsafeRegexError("repeated alternation with overlapping branches")
        for child in _children(op, av):
            _check(child)
    if variable > MAX_VARIABLE_REPEATS:
        raise UnsafeRegexError(f"more than {MAX_VARIABLE_REPEATS} variable-width repeats in one sequence")


def _check_backrefs(items: list, in_repeat: bool, refs: list, repeated_groups: set) -> None:
    for op, av in items:
        if op == _sre_const.GROUPREF:
            refs.append((av, in_repeat))
        if op == _sre_const.SUBPATTERN and in_repeat and av[0] is not None:
            repeated_groups.add(av[0])
        child_repeat = in_repeat or (op in _REPEAT_OPS and av[1] > 1)
        for child in _children(op, av):
            _check_backrefs(child, child_repeat, refs, repeated_groups)


def _check_backreferences(items: list) -> None:
    refs: list = []
    repeated: set = set()
    _check_backrefs(items, False, refs, repeated)
    for group, under_repeat in refs:
        if under_repeat or group in repeated:
            raise UnsafeRegexError("backreference under a repeat (or to a repeated group)")


def _compile(pattern: str):
    """Compiles with the ``regex`` module (supports ``search(timeout=)``); raises ``UnsafeRegexError``."""
    if not isinstance(pattern, str) or not pattern:
        raise UnsafeRegexError("pattern is empty")
    if len(pattern) > MAX_PATTERN_LEN:
        raise UnsafeRegexError(f"pattern is longer than {MAX_PATTERN_LEN} characters")
    translated = translate_pattern(pattern)
    try:
        compiled = _regex.compile(translated, _regex.IGNORECASE)
        tree = _sre_parse.parse(translated, re.IGNORECASE)
    except (re.error, _regex.error, RecursionError, OverflowError) as exc:
        raise UnsafeRegexError(f"invalid regular expression: {exc}") from exc
    return compiled, tree


@functools.lru_cache(maxsize=2048)
def _validated(pattern: str):
    compiled, tree = _compile(pattern)
    items = list(tree)
    _check(items)
    _check_backreferences(items)
    return compiled


def validate_pattern(pattern: str) -> None:
    """Raises ``UnsafeRegexError`` unless the pattern is acceptable (see module docstring)."""
    _validated(pattern)


def compile_pattern(pattern: str):
    """Returns the validated, case-insensitive compiled ``regex`` pattern (cached). Raises ``UnsafeRegexError``."""
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
    """Case-insensitive ``search``; True/False on a result, None for an invalid/unsafe pattern.

    Raises ``BudgetExceeded`` when the budget is spent or the search times out (the budget is then marked exhausted).
    """
    if budget is not None and budget.spent():
        raise BudgetExceeded("evaluation budget spent")
    try:
        compiled = compile_pattern(pattern)
    except UnsafeRegexError:
        return None
    try:
        return compiled.search((text or "")[:REGEX_INPUT_LEN], timeout=SEARCH_TIMEOUT_SECONDS) is not None
    except TimeoutError as exc:
        if budget is not None:
            budget.exhausted = True
        raise BudgetExceeded("regex search timed out") from exc
