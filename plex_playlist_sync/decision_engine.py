"""Arr-style decision engine: custom formats, release profiles, quality definitions, ranking and upgrades.

Semantics follow Lidarr (docs/QUALITY_RESEARCH.md) with the deliberate deviations listed in docs/ARR_PROFILES_SPEC.md:

* Quality order beats format score; format score breaks ties inside a quality tier (spec section 6).
* A custom format matches when, for **each implementation type present**, every ``required`` spec passes and, if the
  type has any non-required specs, at least one of them passes. ``negate`` flips a spec's own result.
* Release profiles: required terms are OR within one profile and AND across profiles; any ignored term rejects.
* Quality definitions are kbps (size per length). Unknown duration never rejects; a track-count *estimate* only
  enforces the maximum.

Every reject and score contribution is recorded in a ``DecisionBreakdown`` (see ``models``).
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from plex_playlist_sync.models import (
    AcquisitionSearchResult,
    DecisionBreakdown,
    EvaluationResult,
    ParsedRelease,
    QualityProfile,
)
from plex_playlist_sync.quality_defaults import (
    entry_label,
    entry_qualities,
    find_cutoff_index,
    legacy_items_to_entries,
    normalize_entries,
)
from plex_playlist_sync.safe_regex import (
    MAX_INPUT_LEN,
    Budget,
    UnsafeRegexError,
    compile_pattern,
    safe_search,
    validate_pattern,
)

logger = logging.getLogger(__name__)

# Protocol preference used as the tie-break after quality, format score and kbps distance. Earlier = preferred.
# A constant for now: delay profiles will drive it per profile later.
PROTOCOL_PREFERENCE: tuple[str, ...] = ("usenet", "torrent", "soulseek")

# One quality tier is worth this many points in ``EvaluationResult.score``; format scores are clamped well below it so
# a higher quality always out-scores a lower one (Lidarr: quality dominates, score is a tie-breaker).
TIER_STEP = 1_000_000
FORMAT_SCORE_CLAMP = TIER_STEP - 1

ESTIMATED_SECONDS_PER_TRACK = 240.0
DEFAULT_PREFERRED_TAG_SCORE = 50

LIDARR_IMPLEMENTATIONS = frozenset(
    {
        "ReleaseTitleSpecification",
        "ReleaseGroupSpecification",
        "IndexerFlagSpecification",
        "SizeSpecification",
    }
)
NATIVE_IMPLEMENTATIONS = frozenset(
    {
        "QualitySpecification",
        "PhraseSpecification",
        "SourceSpecification",
        "ProtocolSpecification",
    }
)
SUPPORTED_IMPLEMENTATIONS = LIDARR_IMPLEMENTATIONS | NATIVE_IMPLEMENTATIONS
REGEX_IMPLEMENTATIONS = frozenset({"ReleaseTitleSpecification", "ReleaseGroupSpecification"})
PROTOCOLS = ("usenet", "torrent", "soulseek")
SOURCES = ("CD", "WEB", "Vinyl", "SACD", "Cassette", "Unknown")


def normalize_protocol(protocol: Optional[str]) -> str:
    proto = str(protocol or "").lower().strip()
    if proto in ("torrent", "torznab"):
        return "torrent"
    if proto in ("usenet", "newznab"):
        return "usenet"
    if proto in ("slskd", "soulseek", "p2p"):
        return "soulseek"
    return proto or "unknown"


def protocol_rank(protocol: Optional[str]) -> int:
    proto = normalize_protocol(protocol)
    try:
        return PROTOCOL_PREFERENCE.index(proto)
    except ValueError:
        return len(PROTOCOL_PREFERENCE)


# --------------------------------------------------------------------------------------------------------------------
# Fuzzy phrase matching (difflib token-set ratio; rapidfuzz is not a dependency)
# --------------------------------------------------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _ratio(a: str, b: str) -> float:
    if not a and not b:
        return 100.0
    return difflib.SequenceMatcher(None, a, b).ratio() * 100.0


def token_set_ratio(a: str, b: str) -> float:
    """fuzzywuzzy-style token set ratio (0-100): robust to word order and extra words in either string."""
    ta = set(_TOKEN_RE.findall((a or "").lower()[:MAX_INPUT_LEN]))
    tb = set(_TOKEN_RE.findall((b or "").lower()[:MAX_INPUT_LEN]))
    if not ta or not tb:
        return 0.0
    sect = " ".join(sorted(ta & tb))
    c1 = (sect + " " + " ".join(sorted(ta - tb))).strip()
    c2 = (sect + " " + " ".join(sorted(tb - ta))).strip()
    scores = [_ratio(c1, c2)]
    if sect:
        scores += [_ratio(sect, c1), _ratio(sect, c2)]
    return max(scores)


def phrase_score(phrase: str, title: str) -> float:
    """Best fuzzy score (0-100) of ``phrase`` inside ``title``: the token-set ratio, or the best order-insensitive
    match of the phrase against any same-sized window of title tokens (tolerates typos in a few words)."""
    best = token_set_ratio(phrase, title)
    p_tokens = _TOKEN_RE.findall((phrase or "").lower())
    t_tokens = _TOKEN_RE.findall((title or "").lower()[:MAX_INPUT_LEN])
    if not p_tokens or not t_tokens:
        return best
    target = " ".join(sorted(p_tokens))
    n = len(p_tokens)
    for size in {max(1, n - 1), n, n + 1}:
        for i in range(0, max(1, len(t_tokens) - size + 1)):
            window = " ".join(sorted(t_tokens[i : i + size]))
            best = max(best, _ratio(target, window))
            if best >= 100:
                return best
    return best


# --------------------------------------------------------------------------------------------------------------------
# Custom format model, normalization and validation
# --------------------------------------------------------------------------------------------------------------------


def _fields_dict(raw: Any) -> dict[str, Any]:
    """Lidarr JSON export uses ``{"value": x}``; the REST API serialises an array of ``{name, value}``."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, list):
        out: dict[str, Any] = {}
        for item in raw:
            if isinstance(item, dict) and item.get("name") is not None:
                out[str(item["name"])] = item.get("value")
        return out
    return {}


def normalize_spec(raw: dict[str, Any]) -> dict[str, Any]:
    impl = str(raw.get("implementation") or "").strip()
    return {
        "name": str(raw.get("name") or impl or "Specification"),
        "implementation": impl,
        "negate": bool(raw.get("negate", False)),
        "required": bool(raw.get("required", False)),
        "fields": _fields_dict(raw.get("fields")),
        "unsupported": impl not in SUPPORTED_IMPLEMENTATIONS,
    }


def normalize_format(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalizes Lidarr-schema (or stored) JSON into ``{name, include_in_rename, specifications, unsupported}``."""
    specs = [normalize_spec(s) for s in (raw.get("specifications") or []) if isinstance(s, dict)]
    rename = raw.get("include_in_rename", raw.get("includeCustomFormatWhenRenaming"))
    return {
        "name": str(raw.get("name") or "").strip(),
        "include_in_rename": bool(rename) if rename is not None else False,
        "specifications": specs,
        "unsupported": any(s["unsupported"] for s in specs),
    }


def validate_format(fmt: dict[str, Any]) -> list[str]:
    """Returns human-readable problems with a normalized format ([] = valid). Unsupported specs are not errors."""
    problems: list[str] = []
    if not fmt.get("name"):
        problems.append("name is required")
    elif len(fmt["name"]) > 120:
        problems.append("name is longer than 120 characters")
    specs = fmt.get("specifications") or []
    if len(specs) > 100:
        problems.append("too many specifications (max 100)")
    for idx, spec in enumerate(specs):
        label = f"specification {idx + 1} ('{spec.get('name')}')"
        impl = spec["implementation"]
        fields = spec["fields"]
        if impl in REGEX_IMPLEMENTATIONS:
            try:
                validate_pattern(str(fields.get("value") or ""))
            except UnsafeRegexError as exc:
                problems.append(f"{label}: {exc}")
        elif impl == "SizeSpecification":
            try:
                lo = float(fields.get("min") or 0)
                hi = float(fields.get("max") or 0)
            except (TypeError, ValueError):
                problems.append(f"{label}: min and max must be numbers (GB)")
                continue
            if lo < 0 or hi < 0:
                problems.append(f"{label}: min and max must not be negative")
            if hi and hi <= lo:
                problems.append(f"{label}: max must be greater than min")
        elif impl == "IndexerFlagSpecification":
            try:
                int(fields.get("value"))
            except (TypeError, ValueError):
                problems.append(f"{label}: value must be an integer flag")
        elif impl == "QualitySpecification":
            from plex_playlist_sync.quality_defaults import QUALITY_ORDER

            if str(fields.get("value")) not in QUALITY_ORDER:
                problems.append(f"{label}: unknown quality '{fields.get('value')}'")
        elif impl == "PhraseSpecification":
            if not str(fields.get("value") or "").strip():
                problems.append(f"{label}: phrase is empty")
            elif len(str(fields["value"])) > 200:
                problems.append(f"{label}: phrase is longer than 200 characters")
            thr = fields.get("threshold", 85)
            try:
                if not 1 <= float(thr) <= 100:
                    problems.append(f"{label}: threshold must be between 1 and 100")
            except (TypeError, ValueError):
                problems.append(f"{label}: threshold must be a number")
        elif impl == "SourceSpecification":
            if str(fields.get("value", "")).strip().lower() not in {s.lower() for s in SOURCES}:
                problems.append(f"{label}: source must be one of {', '.join(SOURCES)}")
        elif impl == "ProtocolSpecification":
            if normalize_protocol(fields.get("value")) not in PROTOCOLS:
                problems.append(f"{label}: protocol must be one of {', '.join(PROTOCOLS)}")
    return problems


def export_format(fmt: dict[str, Any]) -> dict[str, Any]:
    """Lidarr/Servarr-schema JSON for one stored format (fields as the ``{"value": ...}`` object form)."""
    return {
        "name": fmt["name"],
        "includeCustomFormatWhenRenaming": bool(fmt.get("include_in_rename", False)),
        "specifications": [
            {
                "name": s["name"],
                "implementation": s["implementation"],
                "negate": bool(s.get("negate", False)),
                "required": bool(s.get("required", False)),
                "fields": dict(s.get("fields") or {}),
            }
            for s in fmt.get("specifications") or []
        ],
    }


# --------------------------------------------------------------------------------------------------------------------
# Evaluation context and specification matching
# --------------------------------------------------------------------------------------------------------------------


@dataclass
class DurationInfo:
    seconds: float
    estimated: bool = False
    source: str = "tracks"  # "tracks" (known durations) | "estimate" (track-count based)


@dataclass
class DecisionContext:
    title: str
    parsed: ParsedRelease
    size_bytes: Optional[int]
    protocol: str
    indexer_id: Optional[str]
    indexer_name: Optional[str]
    indexer_flags: int
    duration: Optional[DurationInfo]
    budget: Budget
    skipped: int = 0


def _regex_match(ctx: DecisionContext, pattern: str, text: str) -> Optional[bool]:
    result = safe_search(pattern, text, ctx.budget)
    if result is None:
        ctx.skipped += 1
    return result


def _raw_match(spec: dict[str, Any], ctx: DecisionContext) -> Optional[bool]:
    impl = spec["implementation"]
    fields = spec["fields"]
    if impl == "ReleaseTitleSpecification":
        return _regex_match(ctx, str(fields.get("value") or ""), ctx.title)
    if impl == "ReleaseGroupSpecification":
        return _regex_match(ctx, str(fields.get("value") or ""), ctx.parsed.release_group or "")
    if impl == "IndexerFlagSpecification":
        try:
            flag = int(fields.get("value"))
        except (TypeError, ValueError):
            return None
        return (ctx.indexer_flags & flag) == flag
    if impl == "SizeSpecification":
        try:
            lo = float(fields.get("min") or 0)
            hi = float(fields.get("max") or 0)
        except (TypeError, ValueError):
            return None
        size_gb = (ctx.size_bytes or 0) / (1024**3)
        return size_gb > lo and (hi <= 0 or size_gb <= hi)
    if impl == "QualitySpecification":
        return ctx.parsed.quality == str(fields.get("value"))
    if impl == "PhraseSpecification":
        if ctx.budget.spent():
            ctx.skipped += 1
            return None
        try:
            threshold = float(fields.get("threshold", 85))
        except (TypeError, ValueError):
            threshold = 85.0
        return phrase_score(str(fields.get("value") or ""), ctx.title) >= threshold
    if impl == "SourceSpecification":
        wanted = str(fields.get("value") or "").strip().lower()
        actual = (ctx.parsed.source or "Unknown").lower()
        return wanted == actual
    if impl == "ProtocolSpecification":
        return normalize_protocol(fields.get("value")) == ctx.protocol
    return None


def spec_matches(spec: dict[str, Any], ctx: DecisionContext) -> Optional[bool]:
    """Spec result after ``negate``; None = could not be evaluated (unsupported, invalid regex, budget spent)."""
    if spec.get("unsupported"):
        return None
    raw = _raw_match(spec, ctx)
    if raw is None:
        return None
    return raw != bool(spec.get("negate", False))


def format_matches(fmt: dict[str, Any], ctx: DecisionContext) -> bool:
    """Lidarr semantics: per implementation type all required specs pass and at least one non-required passes."""
    if fmt.get("unsupported"):
        return False
    groups: dict[str, list[tuple[bool, Optional[bool]]]] = {}
    for spec in fmt.get("specifications") or []:
        groups.setdefault(spec["implementation"], []).append((bool(spec.get("required")), spec_matches(spec, ctx)))
    if not groups:
        return False
    for results in groups.values():
        required = [r for req, r in results if req]
        optional = [r for req, r in results if not req]
        if any(r is not True for r in required):
            return False
        if optional and not any(r is True for r in optional):
            return False
    return True


# --------------------------------------------------------------------------------------------------------------------
# Release profiles
# --------------------------------------------------------------------------------------------------------------------


def parse_term(term: str) -> tuple[str, str]:
    """``/regex/`` (optional trailing flags are ignored; matching is always case-insensitive) or a plain substring."""
    t = term.strip()
    if len(t) >= 2 and t.startswith("/"):
        end = t.rfind("/")
        if end > 0:
            return "regex", t[1:end]
    return "substring", t


def validate_term(term: str) -> None:
    """Raises ``UnsafeRegexError`` (a ValueError) for an empty term or an unacceptable regex term."""
    if not term.strip():
        raise UnsafeRegexError("term is empty")
    kind, value = parse_term(term)
    if kind == "regex":
        validate_pattern(value)
    elif len(value) > 500:
        raise UnsafeRegexError("term is longer than 500 characters")


def term_matches(term: str, title: str, budget: Optional[Budget] = None) -> bool:
    kind, value = parse_term(term)
    if kind == "substring":
        return value.lower() in title.lower()
    return bool(safe_search(value, title, budget))


# --------------------------------------------------------------------------------------------------------------------
# Prepared profile
# --------------------------------------------------------------------------------------------------------------------


@dataclass
class PreparedProfile:
    profile: QualityProfile
    entries: list[dict[str, Any]]
    cutoff_index: Optional[int]
    tiers: dict[str, int]  # quality -> entry index
    allowed: dict[str, bool]  # quality -> allowed
    formats: list[tuple[dict[str, Any], int]]  # (normalized format with id, score)
    definitions: dict[str, dict[str, Any]]
    release_profiles: list[dict[str, Any]]
    legacy_mode: bool
    has_catalog: bool


def _weights_for_items(profile: QualityProfile) -> dict[str, int]:
    return {i.quality: i.weight for i in profile.items if hasattr(i, "quality")}


def prepare_profile(profile: QualityProfile) -> PreparedProfile:
    legacy_mode = not profile.entries
    entries = normalize_entries(profile.entries) if profile.entries else legacy_items_to_entries(profile.items)
    tiers: dict[str, int] = {}
    allowed: dict[str, bool] = {}
    for idx, entry in enumerate(entries):
        for quality in entry_qualities(entry):
            if quality not in tiers:
                tiers[quality] = idx
                allowed[quality] = bool(entry.get("allowed", True))
    catalog = profile.catalog or {}
    scores = {str(fi.get("format_id")): int(fi.get("score", 0)) for fi in profile.format_items or [] if isinstance(fi, dict)}
    formats: list[tuple[dict[str, Any], int]] = []
    for row in catalog.get("formats") or []:
        fmt = normalize_format(row)
        fmt["id"] = row.get("id")
        formats.append((fmt, scores.get(str(row.get("id")), 0)))
    definitions = {str(d["quality"]): d for d in catalog.get("definitions") or []}
    release_profiles = [
        rp
        for rp in catalog.get("release_profiles") or []
        if rp.get("enabled", True)
        and (not rp.get("quality_profile_ids") or str(profile.id) in {str(i) for i in rp["quality_profile_ids"]})
    ]
    return PreparedProfile(
        profile=profile,
        entries=entries,
        cutoff_index=find_cutoff_index(entries, profile.cutoff),
        tiers=tiers,
        allowed=allowed,
        formats=formats,
        definitions=definitions,
        release_profiles=release_profiles,
        legacy_mode=legacy_mode,
        has_catalog=bool(catalog),
    )


# --------------------------------------------------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------------------------------------------------


def _legacy_pref_matches(tag: str, release: ParsedRelease, raw_lower: str, tags_lower: set[str]) -> bool:
    clean = tag.strip().lower()
    if not clean:
        return False
    source_matches = release.source is not None and clean == release.source.lower()
    return bool(
        source_matches or clean in tags_lower or re.search(r"\b" + re.escape(clean) + r"\b", raw_lower)
    )


def _check_kbps(
    prepared: PreparedProfile,
    quality: str,
    size_bytes: Optional[int],
    duration: Optional[DurationInfo],
    breakdown: DecisionBreakdown,
) -> tuple[bool, Optional[float]]:
    """Quality-definition size-per-length check. Returns (accepted, distance to the preferred kbps)."""
    info: dict[str, Any] = {
        "checked": False,
        "measured": None,
        "estimated": False,
        "duration_seconds": None,
        "duration_source": None,
        "min": None,
        "preferred": None,
        "max": None,
        "skipped_reason": None,
    }
    breakdown.kbps = info
    definition = prepared.definitions.get(quality)
    if definition is None:
        info["skipped_reason"] = "no quality definition"
        return True, None
    info["min"] = definition.get("min_kbps")
    info["preferred"] = definition.get("preferred_kbps")
    info["max"] = definition.get("max_kbps")
    if not size_bytes or size_bytes <= 0:
        info["skipped_reason"] = "release size unknown"
        return True, None
    if duration is None or duration.seconds <= 0:
        info["skipped_reason"] = "duration unknown"
        return True, None
    measured = size_bytes * 8 / 1000 / duration.seconds
    info.update(
        checked=True,
        measured=round(measured, 1),
        estimated=duration.estimated,
        duration_seconds=round(duration.seconds, 1),
        duration_source=duration.source,
    )
    ok = True
    lo, hi = definition.get("min_kbps"), definition.get("max_kbps")
    # Estimated durations only ever enforce the maximum, to avoid false rejections (spec section 1).
    if lo is not None and lo > 0 and not duration.estimated and measured < float(lo):
        breakdown.rejections.append(
            {
                "code": "kbps_below_min",
                "message": f"Bitrate {measured:.0f} kbps is below the {quality} minimum ({float(lo):.0f} kbps)",
            }
        )
        ok = False
    if hi is not None and hi > 0 and measured > float(hi):
        suffix = " (estimated duration)" if duration.estimated else ""
        breakdown.rejections.append(
            {
                "code": "kbps_above_max",
                "message": f"Bitrate {measured:.0f} kbps{suffix} exceeds the {quality} maximum ({float(hi):.0f} kbps)",
            }
        )
        ok = False
    pref = definition.get("preferred_kbps")
    distance = abs(measured - float(pref)) if pref else None
    return ok, distance


def evaluate_prepared(
    release: ParsedRelease,
    prepared: PreparedProfile,
    size_bytes: Optional[int] = None,
    *,
    protocol: Optional[str] = None,
    indexer_id: Optional[Any] = None,
    indexer_name: Optional[str] = None,
    indexer_flags: int = 0,
    duration: Optional[DurationInfo] = None,
    budget: Optional[Budget] = None,
) -> EvaluationResult:
    profile = prepared.profile
    title = release.raw_title
    bd = DecisionBreakdown(
        title=title,
        release_group=release.release_group,
        protocol=normalize_protocol(protocol) if protocol else None,
        source=release.source,
        quality=release.quality,
        min_format_score=int(profile.min_format_score),
        cutoff_format_score=int(profile.cutoff_format_score),
    )
    ctx = DecisionContext(
        title=title,
        parsed=release,
        size_bytes=size_bytes,
        protocol=bd.protocol or "unknown",
        indexer_id=str(indexer_id) if indexer_id is not None else None,
        indexer_name=indexer_name,
        indexer_flags=int(indexer_flags or 0),
        duration=duration,
        budget=budget or Budget(),
    )

    def reject(code: str, message: str) -> None:
        bd.rejections.append({"code": code, "message": message})

    # 1. Legacy size bounds (columns kept read-only; Quality Definitions are the supported control).
    if size_bytes is not None:
        size_mb = size_bytes / (1024 * 1024)
        if profile.min_size_mb is not None and size_mb < profile.min_size_mb:
            reject("size_below_min", f"Release size ({size_mb:.1f} MB) is below minimum ({profile.min_size_mb:.1f} MB)")
        if profile.max_size_mb is not None and size_mb > profile.max_size_mb:
            reject("size_above_max", f"Release size ({size_mb:.1f} MB) exceeds maximum ({profile.max_size_mb:.1f} MB)")

    raw_lower = title.lower()
    tags_lower = {t.lower() for t in release.tags}

    # 2. Release profiles (required terms OR within a profile, AND across profiles; any ignored term rejects).
    for rp in prepared.release_profiles:
        allowed_indexers = [str(i) for i in rp.get("indexer_ids") or []]
        if allowed_indexers and (ctx.indexer_id is None or ctx.indexer_id not in allowed_indexers):
            continue
        rp_name = str(rp.get("name") or f"#{rp.get('id')}")
        outcome = {"id": rp.get("id"), "name": rp_name, "result": "pass", "detail": ""}
        required = [t for t in rp.get("required") or [] if str(t).strip()]
        ignored = [t for t in rp.get("ignored") or [] if str(t).strip()]
        if required and not any(term_matches(t, title, ctx.budget) for t in required):
            outcome.update(result="rejected", detail="missing required term")
            reject(
                "release_profile_required",
                f"Release profile '{rp_name}' requires one of: {', '.join(str(t) for t in required)}",
            )
        for term in ignored:
            if term_matches(term, title, ctx.budget):
                outcome.update(result="rejected", detail=f"ignored term {term}")
                reject("release_profile_ignored", f"Release profile '{rp_name}' ignores term '{term}'")
                break
        bd.release_profiles.append(outcome)

    # 3. Legacy ignored tags (profiles that have not been migrated to release profiles).
    if prepared.legacy_mode:
        for tag in profile.ignored_tags:
            clean_tag = tag.strip().lower()
            if not clean_tag:
                continue
            if clean_tag in tags_lower or re.search(r"\b" + re.escape(clean_tag) + r"\b", raw_lower):
                reject("legacy_ignored_tag", f"Contains rejected keyword '{tag}'")

    # 4. Quality allowed in profile.
    tier = prepared.tiers.get(release.quality)
    bd.tier = tier
    bd.tier_name = entry_label(prepared.entries[tier]) if tier is not None else None
    bd.quality_allowed = bool(tier is not None and prepared.allowed.get(release.quality, False))
    bd.cutoff_tier = prepared.cutoff_index
    if not bd.quality_allowed:
        reject("quality_not_allowed", f"Quality '{release.quality}' is not allowed in profile")

    # 5. Quality definition: kbps (size per length).
    kbps_ok, distance = _check_kbps(prepared, release.quality, size_bytes, duration, bd)
    del kbps_ok  # rejections were appended to the breakdown

    # 6. Custom formats -> score.
    catalog_score = 0
    for fmt, score in prepared.formats:
        if format_matches(fmt, ctx):
            catalog_score += score
            bd.matched_formats.append({"id": fmt.get("id"), "name": fmt["name"], "score": score})
    legacy_score = 0
    if prepared.legacy_mode:
        for pref in profile.preferred_tags:
            if _legacy_pref_matches(pref, release, raw_lower, tags_lower):
                legacy_score += DEFAULT_PREFERRED_TAG_SCORE
                bd.matched_formats.append(
                    {"id": None, "name": f"Preferred tag: {pref.strip()}", "score": DEFAULT_PREFERRED_TAG_SCORE}
                )
    for cf in profile.custom_formats or []:
        name = cf.get("name", "Custom Format")
        try:
            score = int(cf.get("score", 0))
        except (ValueError, TypeError):
            score = 0
        pattern_str = cf.get("pattern", "")
        if not pattern_str:
            continue
        matched = safe_search(str(pattern_str), title, ctx.budget)
        if matched is None:
            logger.warning("Skipped legacy custom format '%s': invalid, unsafe or over the time budget", name)
            continue
        if matched != bool(cf.get("negate", False)):
            legacy_score += score
            bd.matched_formats.append({"id": None, "name": str(name), "score": score})

    format_score = catalog_score + legacy_score
    bd.format_score = format_score
    clamped = max(-FORMAT_SCORE_CLAMP, min(FORMAT_SCORE_CLAMP, format_score))
    total = ((len(prepared.entries) - tier) * TIER_STEP if tier is not None else 0) + clamped
    bd.total_score = total

    if prepared.formats and catalog_score < profile.min_format_score:
        reject(
            "format_score_below_min",
            f"Custom format score {catalog_score} is below profile minimum {profile.min_format_score}",
        )

    # Legacy ``min_score`` (pre-v49): compared against weight + format score as before.
    if profile.min_score is not None:
        weights = _weights_for_items(profile)
        for entry in prepared.entries:
            if entry.get("type") == "quality" and isinstance(entry.get("weight"), int):
                weights[entry["quality"]] = entry["weight"]
        if release.quality in weights:
            base = weights[release.quality]
        elif tier is not None:
            base = (len(prepared.entries) - tier) * 100
        else:
            base = 0
        legacy_total = base + format_score
        if legacy_total < profile.min_score:
            reject("legacy_min_score", f"Score {legacy_total} is below profile minimum {profile.min_score}")

    if ctx.skipped:
        bd.notes.append(f"{ctx.skipped} regex/phrase check(s) skipped (invalid pattern or time budget spent)")

    # 7. Cutoff.
    quality_cutoff_met = bool(tier is not None and prepared.cutoff_index is not None and tier <= prepared.cutoff_index)
    bd.quality_cutoff_met = quality_cutoff_met
    meets_cutoff = quality_cutoff_met and (profile.cutoff_format_score <= 0 or format_score >= profile.cutoff_format_score)

    return EvaluationResult(
        is_acceptable=not bd.rejections,
        score=total,
        rejection_reasons=[r["message"] for r in bd.rejections],
        parsed_quality=release.quality,
        meets_cutoff=meets_cutoff,
        format_score=format_score,
        tier=tier,
        kbps_distance=distance,
        breakdown=bd,
    )


def evaluate_release_with_context(
    release: ParsedRelease,
    profile: QualityProfile,
    size_bytes: Optional[int] = None,
    **kwargs: Any,
) -> EvaluationResult:
    return evaluate_prepared(release, prepare_profile(profile), size_bytes, **kwargs)


def candidate_context(candidate: AcquisitionSearchResult) -> dict[str, Any]:
    """Keyword arguments for ``evaluate_prepared`` taken from a search result."""
    extra = candidate.extra if isinstance(candidate.extra, dict) else {}
    flags = extra.get("indexer_flags", 0)
    try:
        flags = int(flags or 0)
    except (TypeError, ValueError):
        flags = 0
    return {
        "protocol": candidate.protocol,
        "indexer_id": extra.get("indexer_id"),
        "indexer_name": extra.get("indexer_name"),
        "indexer_flags": flags,
    }


# --------------------------------------------------------------------------------------------------------------------
# Ranking and upgrades
# --------------------------------------------------------------------------------------------------------------------

_NO_DISTANCE = 1e12


def rank_key(protocol: Optional[str], seeders: Optional[int], result: EvaluationResult) -> tuple[Any, ...]:
    """Sort key (sort descending): quality order > format score > preferred-kbps distance > protocol > seeders."""
    tier = result.tier if result.tier is not None else 10**9
    distance = result.kbps_distance if result.kbps_distance is not None else _NO_DISTANCE
    return (-tier, result.format_score, -distance, -protocol_rank(protocol), int(seeders or 0))


@dataclass
class UpgradeDecision:
    is_upgrade: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"is_upgrade": self.is_upgrade, "reason": self.reason}


def evaluate_upgrade(current: EvaluationResult, candidate: EvaluationResult, profile: QualityProfile) -> UpgradeDecision:
    """Spec section 6, step 8: upgrade while below the cutoff quality or the cutoff format score, and only if the
    candidate is a better quality, or the same quality with a format score higher by ``min_upgrade_format_score``."""
    if not profile.upgrade_allowed:
        return UpgradeDecision(False, "Upgrades are disabled for this profile")
    if not candidate.is_acceptable:
        return UpgradeDecision(False, "Candidate is rejected by the profile")
    if current.meets_cutoff:
        return UpgradeDecision(False, "Current file already meets the cutoff")
    if candidate.tier is None:
        return UpgradeDecision(False, "Candidate quality is not in the profile")
    if current.tier is None or candidate.tier < current.tier:
        return UpgradeDecision(True, "Candidate is a higher quality")
    if candidate.tier > current.tier:
        return UpgradeDecision(False, "Candidate is a lower quality than the current file")
    needed = max(1, int(profile.min_upgrade_format_score))
    gain = candidate.format_score - current.format_score
    if gain >= needed:
        return UpgradeDecision(True, f"Format score improves by {gain} (needs {needed})")
    return UpgradeDecision(False, f"Format score improves by {gain}, below the required {needed}")


def upgrade_floor(current: EvaluationResult, profile: QualityProfile) -> int:
    """Score a candidate must *exceed* to count as an upgrade (callers compare ``candidate.score > floor``).

    Quality tiers are ``TIER_STEP`` apart, so a better quality always clears the floor; inside a tier the candidate
    needs ``min_upgrade_format_score`` more format score than the current file.
    """
    return current.score + max(1, int(profile.min_upgrade_format_score)) - 1


def resolve_durations(
    track_durations: Iterable[Optional[float]], total_tracks: Optional[int]
) -> Optional[DurationInfo]:
    """Album duration: known track durations summed; partial knowledge is extrapolated; else ``total_tracks`` x 240 s
    (flagged estimated). None when nothing is known."""
    durations = list(track_durations)
    known = [float(d) for d in durations if d is not None and float(d) > 0]
    rows = len(durations)
    total = max(int(total_tracks or 0), rows)
    if known and len(known) >= total:
        return DurationInfo(sum(known), estimated=False, source="tracks")
    if known:
        avg = sum(known) / len(known)
        return DurationInfo(sum(known) + avg * (total - len(known)), estimated=True, source="estimate")
    if total > 0:
        return DurationInfo(total * ESTIMATED_SECONDS_PER_TRACK, estimated=True, source="estimate")
    return None
