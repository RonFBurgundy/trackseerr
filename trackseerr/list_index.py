"""Sort-name normalization and the SQL group index behind the scrubber rail.

Two pieces that have to agree exactly:

* ``library_sort_key`` is the single normalizer used both when a ``sort_name`` / ``sort_title`` column is written and
  when a group label is derived, so ``ORDER BY`` and the index group on the very same stored key.
* ``build_index`` turns a ``FROM ... WHERE ...`` fragment plus a ``SortDef`` into ``[{label, offset, count}]`` in the
  list's own order. Groups are aggregated by SQLite (``GROUP BY``), and offsets are the running sum of counts in the
  order the list is returned, so ``offset`` is the exact position of the group's first row for the same filters.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional, Sequence

NULL_LABEL = "—"  # em dash: the group for rows with no value
QUANTILE_BUCKETS = 10
MONTH_SPAN_LIMIT = 24  # month buckets when the dates span this many months or fewer
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_ARTICLE_RE = re.compile(r"^(?:THE|AN|A)\s+")
# Letters NFKD does not decompose; folded so "Ørsted" files under O and "Æon" under A.
_FOLD = str.maketrans({"Ø": "O", "Æ": "AE", "Œ": "OE", "Đ": "D", "Ð": "D", "Ł": "L", "Þ": "TH"})


def library_sort_key(text: Any) -> str:
    """The persisted sort key: accent-folded, upper-cased, without a leading "The "/"A "/"An ".

    A key that does not start with A-Z is prefixed with ``#``, so every digit, symbol, emoji and non-Latin name sorts
    (BINARY collation) into one contiguous block ahead of the letters. The empty name becomes ``#``.
    """
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).upper().translate(_FOLD).strip()
    without_article = _ARTICLE_RE.sub("", folded, count=1).lstrip()
    key = without_article or folded
    if not key or not ("A" <= key[0] <= "Z"):
        return "#" + key
    return key


def fold_search_text(text: Any) -> str:
    """Search-side normalizer: accents stripped, ligature-like letters expanded, case-folded.

    Mirrors the folding ``library_sort_key`` applies (minus the article and ``#`` rules), so "ERIC" finds "Éric",
    "orsted" finds "Ørsted" and "eclat" finds "Éclat". Registered as the SQLite function ``fold_text`` for native lists.
    """
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.upper().translate(_FOLD).casefold()


def group_label_for_key(key: Optional[str]) -> str:
    """The scrubber label of a stored sort key: its first letter A-Z, else ``#``."""
    if key and "A" <= key[0] <= "Z":
        return key[0]
    return "#"


def group_label_for_name(text: Any) -> str:
    """The scrubber label a display name files under (what a client or test computes for a record)."""
    return group_label_for_key(library_sort_key(text))


def escape_like(text: str) -> str:
    """Escapes ``%``, ``_`` and the escape character itself for a ``LIKE ... ESCAPE '\\'`` pattern."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass(frozen=True)
class SortDef:
    """One whitelisted sort: the SQL expression ordered by, and how its values are grouped.

    ``kind``: ``name`` (the expression is a persisted sort key), ``date`` (text starting ``YYYY[-MM]``), ``size``
    (bytes) or ``count`` (an integer; ``unit`` is the plural noun used in labels).
    """

    expr: str
    kind: str
    unit: str = ""


def order_clause(sort: SortDef, sort_dir: str, tiebreak: str) -> str:
    direction = "DESC" if str(sort_dir).lower() == "desc" else "ASC"
    return f"ORDER BY {sort.expr} {direction}, {tiebreak} {direction}"


# ------------------------------------------------------------------------------------------------------- labels


def _fmt_number(value: float) -> str:
    text = f"{value:.0f}" if value >= 10 else f"{value:.1f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _size_parts(num_bytes: int) -> tuple[str, str]:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return _fmt_number(value), unit
        value /= 1024
    return _fmt_number(value), "GB"  # unreachable; keeps the type checker honest


def format_size(num_bytes: int) -> str:
    amount, unit = _size_parts(num_bytes)
    return f"{amount} {unit}"


def _size_range(low: int, high: int) -> str:
    low_amount, low_unit = _size_parts(low)
    high_amount, high_unit = _size_parts(high)
    if low_unit == high_unit:
        return f"{low_amount}–{high_amount} {low_unit}"
    return f"{low_amount} {low_unit}–{high_amount} {high_unit}"


def _count_label(low: int, high: Optional[int], unit: str) -> str:
    def noun(n: int) -> str:
        return unit[:-1] if n == 1 and unit.endswith("s") else unit

    if high is None:
        return f"{low}+ {unit}"
    if low == high:
        return f"{low} {noun(low)}"
    return f"{low}\u2013{high} {unit}"


def _date_label(key: str) -> str:
    """``2026-03`` -> ``Mar 2026``; a year (or anything unparseable) is shown as-is."""
    if len(key) == 7 and key[4] == "-" and key[:4].isdigit() and key[5:].isdigit():
        month = int(key[5:])
        if 1 <= month <= 12:
            return f"{_MONTHS[month - 1]} {key[:4]}"
    return key


# --------------------------------------------------------------------------------------------- group builders
# Each builder returns ascending-order groups as ``[(label, count)]`` with the NULL group first (SQLite's NULLs
# are the smallest values), which ``build_index`` reverses for a descending list.


def _name_groups(conn: sqlite3.Connection, frm: str, params: Sequence[Any], expr: str) -> list[tuple[str, int]]:
    rows = conn.execute(
        f"SELECT SUBSTR({expr}, 1, 1) AS k, COUNT(*) AS c {frm} GROUP BY k ORDER BY k", list(params)
    ).fetchall()
    groups: list[tuple[str, int]] = []
    for key, count in rows:
        label = group_label_for_key(key)
        if groups and groups[-1][0] == label:
            groups[-1] = (label, groups[-1][1] + int(count))
        else:
            groups.append((label, int(count)))
    return groups


def _year_month(value: str) -> Optional[tuple[int, int]]:
    if len(value) >= 4 and value[:4].isdigit():
        month = int(value[5:7]) if len(value) >= 7 and value[4] == "-" and value[5:7].isdigit() else 1
        return int(value[:4]), month
    return None


def _date_groups(conn: sqlite3.Connection, frm: str, params: Sequence[Any], expr: str) -> list[tuple[str, int]]:
    bounds = conn.execute(
        f"SELECT MIN(d), MAX(d) FROM (SELECT NULLIF({expr}, '') AS d {frm}) WHERE d IS NOT NULL", list(params)
    ).fetchone()
    width = 4
    if bounds and bounds[0] is not None and bounds[1] is not None:
        low, high = _year_month(str(bounds[0])), _year_month(str(bounds[1]))
        if low and high and (high[0] - low[0]) * 12 + (high[1] - low[1]) <= MONTH_SPAN_LIMIT:
            width = 7
    rows = conn.execute(
        f"SELECT SUBSTR(NULLIF({expr}, ''), 1, {width}) AS k, COUNT(*) AS c {frm} GROUP BY k ORDER BY k",
        list(params),
    ).fetchall()
    groups: list[tuple[str, int]] = []
    for key, count in rows:
        label = NULL_LABEL if key is None else _date_label(str(key))
        if groups and groups[-1][0] == label:
            groups[-1] = (label, groups[-1][1] + int(count))
        else:
            groups.append((label, int(count)))
    return groups


def _number_groups(
    conn: sqlite3.Connection, frm: str, params: Sequence[Any], sort: SortDef
) -> list[tuple[str, int]]:
    base = f"SELECT {sort.expr} AS v {frm}"
    null_count = int(conn.execute(f"SELECT COUNT(*) FROM ({base}) WHERE v IS NULL", list(params)).fetchone()[0])
    # Quantile bucket per DISTINCT value (cumulative count before it, scaled to 0..9), so equal values never split.
    rows = conn.execute(
        f"""
        WITH base AS ({base}),
        dist AS (SELECT v, COUNT(*) AS c FROM base WHERE v IS NOT NULL GROUP BY v),
        cum AS (SELECT v, c, SUM(c) OVER (ORDER BY v) - c AS before FROM dist)
        SELECT (before * {QUANTILE_BUCKETS}) / (SELECT SUM(c) FROM dist) AS b, MIN(v), MAX(v), SUM(c)
        FROM cum GROUP BY b ORDER BY b
        """,
        list(params),
    ).fetchall()
    buckets = [(int(lo), int(hi), int(c)) for _b, lo, hi, c in rows]
    groups: list[tuple[str, int]] = []
    if null_count:
        groups.append((NULL_LABEL, null_count))
    for i, (low, high, count) in enumerate(buckets):
        following = buckets[i + 1][0] if i + 1 < len(buckets) else None
        if sort.kind == "size":
            if len(buckets) == 1:
                label = format_size(low) if low == high else _size_range(low, high)
            elif i == 0:
                label = f"<{format_size(following)}" if following is not None else format_size(low)
            elif following is None:
                label = f"≥{format_size(low)}"
            else:
                label = _size_range(low, following)
        else:
            if following is not None:
                label = _count_label(low, following - 1, sort.unit)
            else:
                label = _count_label(low, None if high > low else low, sort.unit)
        groups.append((label, count))
    return groups


def build_index(
    conn: sqlite3.Connection, frm: str, params: Sequence[Any], sort: SortDef, sort_dir: str
) -> tuple[int, list[dict[str, Any]]]:
    """``(total, groups)`` for ``frm`` (a ``FROM ... [WHERE ...]`` fragment) in the list order of ``sort``/``sort_dir``."""
    if sort.kind == "name":
        asc = _name_groups(conn, frm, params, sort.expr)
    elif sort.kind == "date":
        asc = _date_groups(conn, frm, params, sort.expr)
    else:
        asc = _number_groups(conn, frm, params, sort)
    ordered = list(reversed(asc)) if str(sort_dir).lower() == "desc" else asc
    groups: list[dict[str, Any]] = []
    offset = 0
    for label, count in ordered:
        groups.append({"label": label, "offset": offset, "count": count})
        offset += count
    return offset, groups
