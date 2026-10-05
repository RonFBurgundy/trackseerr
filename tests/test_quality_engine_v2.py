"""Decision engine: custom formats, release profiles, kbps checks, ranking, upgrades, regex safety, breakdown."""

import time

import pytest

from plex_playlist_sync.acquisition_coordinator import AcquisitionCoordinator, _to_quality_profile
from plex_playlist_sync.decision_engine import (
    DurationInfo,
    PROTOCOL_PREFERENCE,
    evaluate_upgrade,
    export_format,
    normalize_format,
    protocol_rank,
    rank_key,
    resolve_durations,
    token_set_ratio,
    upgrade_floor,
    validate_format,
)
from plex_playlist_sync.models import AcquisitionSearchResult
from plex_playlist_sync.quality import evaluate_release, extract_release_group, parse_release_title
from plex_playlist_sync.safe_regex import Budget, UnsafeRegexError, safe_search, validate_pattern
from plex_playlist_sync.storage import Database

MB = 1024 * 1024


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def _profile(entries, cutoff, formats=None, release_profiles=None, definitions=None, scores=None, **kw):
    """Hand-built v2 profile with an explicit catalog (formats get ids 1..n)."""
    fmts = []
    for i, f in enumerate(formats or [], start=1):
        fmts.append({"id": i, **f})
    data = {
        "id": "p1",
        "name": "P1",
        "cutoff": cutoff,
        "items": entries,
        "format_items": [{"format_id": i, "score": s} for i, s in (scores or {}).items()],
        "catalog": {"definitions": definitions or [], "formats": fmts, "release_profiles": release_profiles or []},
    }
    data.update(kw)
    return _to_quality_profile(data)


def _q(name, allowed=True):
    return {"type": "quality", "quality": name, "allowed": allowed}


FLAC_MP3 = [_q("FLAC 24bit"), _q("FLAC 16bit"), _q("MP3 320"), _q("MP3 V0")]


def _spec(impl, value=None, negate=False, required=False, **fields):
    f = dict(fields)
    if value is not None:
        f["value"] = value
    return {"name": impl, "implementation": impl, "negate": negate, "required": required, "fields": f}


def _fmt(*specs, name="F"):
    return {"name": name, "specifications": list(specs)}


def _score(profile, title, **kw):
    return evaluate_release(parse_release_title(title), profile, **kw)


# ------------------------------------------------------------------ specifications


def test_release_title_spec_regex_case_insensitive():
    p = _profile(FLAC_MP3, "FLAC 16bit", [_fmt(_spec("ReleaseTitleSpecification", r"\bvinyl\b"))], scores={1: -5})
    r = _score(p, "A - B [FLAC] VINYL")
    assert r.format_score == -5
    assert r.breakdown.matched_formats == [{"id": 1, "name": "F", "score": -5}]
    assert _score(p, "A - B [FLAC] CD").format_score == 0


def test_negate_inverts_result():
    p = _profile(FLAC_MP3, "FLAC 16bit", [_fmt(_spec("ReleaseTitleSpecification", r"\blive\b", negate=True))], scores={1: 7})
    assert _score(p, "A - B [FLAC]").format_score == 7
    assert _score(p, "A - B Live [FLAC]").format_score == 0


def test_required_and_optional_semantics():
    # Two optional specs (OR) + one required (AND) of the same implementation.
    fmt = _fmt(
        _spec("ReleaseTitleSpecification", r"\bcd\b"),
        _spec("ReleaseTitleSpecification", r"\bweb\b"),
        _spec("ReleaseTitleSpecification", r"flac", required=True),
    )
    p = _profile(FLAC_MP3, "FLAC 16bit", [fmt], scores={1: 10})
    assert _score(p, "A - B FLAC CD").format_score == 10
    assert _score(p, "A - B FLAC WEB").format_score == 10
    assert _score(p, "A - B FLAC Vinyl").format_score == 0  # no optional matched
    assert _score(p, "A - B CD MP3 320").format_score == 0  # required failed


def test_different_implementations_are_anded():
    fmt = _fmt(_spec("ReleaseTitleSpecification", r"flac"), _spec("ProtocolSpecification", "usenet"))
    p = _profile(FLAC_MP3, "FLAC 16bit", [fmt], scores={1: 3})
    assert _score(p, "A - B FLAC", protocol="usenet").format_score == 3
    assert _score(p, "A - B FLAC", protocol="torrent").format_score == 0


def test_release_group_spec_and_negate_on_missing_group():
    fmt = _fmt(_spec("ReleaseGroupSpecification", r"\bDeVOiD\b"))
    p = _profile(FLAC_MP3, "FLAC 16bit", [fmt], scores={1: 100})
    assert _score(p, "Artist-Album-2020-FLAC-DeVOiD").format_score == 100
    assert _score(p, "Artist - Album [FLAC]").format_score == 0
    neg = _profile(FLAC_MP3, "FLAC 16bit", [_fmt(_spec("ReleaseGroupSpecification", r"BadGroup", negate=True))], scores={1: 4})
    assert _score(neg, "Artist - Album [FLAC]").format_score == 4


def test_size_spec_gb_range_exclusive_min_inclusive_max():
    fmt = _fmt(_spec("SizeSpecification", min=1, max=2))
    p = _profile(FLAC_MP3, "FLAC 16bit", [fmt], scores={1: 9})
    gb = 1024**3
    assert _score(p, "A - B [FLAC]", size_bytes=int(1.5 * gb)).format_score == 9
    assert _score(p, "A - B [FLAC]", size_bytes=2 * gb).format_score == 9  # max inclusive
    assert _score(p, "A - B [FLAC]", size_bytes=1 * gb).format_score == 0  # min exclusive
    assert _score(p, "A - B [FLAC]", size_bytes=None).format_score == 0


def test_indexer_flag_spec_bitmask():
    fmt = _fmt(_spec("IndexerFlagSpecification", 4))
    p = _profile(FLAC_MP3, "FLAC 16bit", [fmt], scores={1: 6})
    assert _score(p, "A - B [FLAC]", indexer_flags=5).format_score == 6
    assert _score(p, "A - B [FLAC]", indexer_flags=3).format_score == 0


def test_native_quality_source_protocol_specs():
    p = _profile(
        FLAC_MP3,
        "FLAC 16bit",
        [
            _fmt(_spec("QualitySpecification", "FLAC 24bit"), name="Q"),
            _fmt(_spec("SourceSpecification", "Vinyl"), name="S"),
            _fmt(_spec("ProtocolSpecification", "soulseek"), name="P"),
        ],
        scores={1: 1, 2: 20, 3: 300},
    )
    r = _score(p, "A - B [24bit] Vinyl", protocol="slskd")
    assert r.format_score == 321
    assert {m["name"] for m in r.breakdown.matched_formats} == {"Q", "S", "P"}
    assert _score(p, "A - B [FLAC 16bit] CD", protocol="torrent").format_score == 0


def test_phrase_spec_fuzzy_threshold():
    fmt = _fmt(_spec("PhraseSpecification", "deluxe edition", threshold=85))
    p = _profile(FLAC_MP3, "FLAC 16bit", [fmt], scores={1: 5})
    assert _score(p, "Artist - Album (Edition Deluxe) [FLAC]").format_score == 5  # word order ignored
    assert _score(p, "Artist - Album (Delux Edition) [FLAC]").format_score == 5  # typo tolerated
    assert _score(p, "Artist - Album [FLAC]").format_score == 0
    assert token_set_ratio("deluxe edition", "totally different words") < 50


def test_unsupported_implementation_flagged_and_ignored():
    raw = {"name": "X", "specifications": [{"name": "r", "implementation": "ResolutionSpecification", "fields": {"value": 1}}]}
    fmt = normalize_format(raw)
    assert fmt["unsupported"] is True and fmt["specifications"][0]["unsupported"] is True
    assert validate_format(fmt) == []
    p = _profile(FLAC_MP3, "FLAC 16bit", [raw], scores={1: 50})
    assert _score(p, "A - B [FLAC]").format_score == 0


def test_invalid_regex_rejected_by_validation():
    fmt = normalize_format(_fmt(_spec("ReleaseTitleSpecification", "(unclosed"), name="Bad"))
    assert any("invalid regular expression" in e for e in validate_format(fmt))


# ------------------------------------------------------------------ Lidarr JSON round trip


SERVARR_EXAMPLE = {
    "name": "Preferred Groups",
    "includeCustomFormatWhenRenaming": False,
    "specifications": [
        {"name": "DeVOiD", "implementation": "ReleaseGroupSpecification", "negate": False, "required": False,
         "fields": {"value": "\\bDeVOiD\\b"}},
        {"name": "PERFECT", "implementation": "ReleaseGroupSpecification", "negate": False, "required": False,
         "fields": {"value": "\\bPERFECT\\b"}},
        {"name": "ENRiCH", "implementation": "ReleaseGroupSpecification", "negate": False, "required": False,
         "fields": {"value": "\\bENRiCH\\b"}},
    ],
}


def test_lidarr_json_round_trip_and_array_fields():
    fmt = normalize_format(SERVARR_EXAMPLE)
    assert validate_format(fmt) == []
    assert export_format(fmt) == SERVARR_EXAMPLE
    api_style = {**SERVARR_EXAMPLE, "specifications": [
        {**s, "fields": [{"name": "value", "value": s["fields"]["value"]}]} for s in SERVARR_EXAMPLE["specifications"]
    ]}
    assert export_format(normalize_format(api_style)) == SERVARR_EXAMPLE
    p = _profile(FLAC_MP3, "FLAC 16bit", [SERVARR_EXAMPLE], scores={1: 100})
    assert _score(p, "Artist-Album-2020-FLAC-ENRiCH").format_score == 100


# ------------------------------------------------------------------ release group extraction


@pytest.mark.parametrize(
    "title,group",
    [
        ("Artist-Album-2020-FLAC-DeVOiD", "DeVOiD"),
        ("Artist-Album-WEB-2020-ENRiCH", "ENRiCH"),
        ("Artist - Album (2020) [FLAC] [PERFECT]", "PERFECT"),
        ("Artist - Album (2020) (LoRD)", "LoRD"),
        ("Artist - Album [FLAC]", None),
        ("Artist - Album - FLAC", None),
        ("Artist - Album (2001) [WEB]", None),
    ],
)
def test_release_group_extraction(title, group):
    assert extract_release_group(title) == group
    assert parse_release_title(title).release_group == group


# ------------------------------------------------------------------ release profiles


def _rp(name="RP", required=None, ignored=None, **kw):
    return {"id": 1, "name": name, "enabled": True, "required": required or [], "ignored": ignored or [],
            "indexer_ids": [], "quality_profile_ids": [], **kw}


def test_release_profile_ignored_regex_and_substring():
    p = _profile(FLAC_MP3, "FLAC 16bit", release_profiles=[_rp(ignored=["fake flac", "/\\bMQA\\b/"])])
    assert _score(p, "A - B [FLAC] fake FLAC").is_acceptable is False
    r = _score(p, "A - B [FLAC] mqa")
    assert not r.is_acceptable
    assert r.breakdown.rejections[0]["code"] == "release_profile_ignored"
    assert "RP" in r.rejection_reasons[0]
    assert _score(p, "A - B [FLAC]").is_acceptable is True


def test_release_profile_required_or_within_and_across():
    p = _profile(
        FLAC_MP3,
        "FLAC 16bit",
        release_profiles=[_rp("one", required=["cd", "/web/"]), _rp("two", required=["flac"], id=2)],
    )
    assert _score(p, "A - B FLAC CD").is_acceptable is True  # OR inside "one", "two" satisfied
    assert _score(p, "A - B FLAC WEB").is_acceptable is True
    r = _score(p, "A - B FLAC Vinyl")  # "one" fails
    assert r.is_acceptable is False and "'one'" in r.rejection_reasons[0]
    r = _score(p, "A - B CD [MP3 320]")  # "one" ok, "two" fails (AND across profiles)
    assert r.is_acceptable is False and "'two'" in "".join(r.rejection_reasons)


def test_release_profile_scoped_to_indexer_and_quality_profile():
    p = _profile(FLAC_MP3, "FLAC 16bit", release_profiles=[_rp(ignored=["bad"], indexer_ids=["7"])])
    assert _score(p, "A - B [FLAC] bad", indexer_id=7).is_acceptable is False
    assert _score(p, "A - B [FLAC] bad", indexer_id=8).is_acceptable is True
    assert _score(p, "A - B [FLAC] bad").is_acceptable is True
    other = _profile(FLAC_MP3, "FLAC 16bit", release_profiles=[_rp(ignored=["bad"], quality_profile_ids=["zzz"])])
    assert _score(other, "A - B [FLAC] bad").is_acceptable is True


def test_disabled_release_profile_is_ignored():
    p = _profile(FLAC_MP3, "FLAC 16bit", release_profiles=[_rp(ignored=["bad"], enabled=False)])
    assert _score(p, "A - B [FLAC] bad").is_acceptable is True


# ------------------------------------------------------------------ kbps


DEFS = [
    {"quality": "FLAC 16bit", "min_kbps": 0.0, "preferred_kbps": 895.0, "max_kbps": 1400.0},
    {"quality": "MP3 320", "min_kbps": 290.0, "preferred_kbps": 320.0, "max_kbps": 350.0},
]


def _size(kbps, seconds):
    return int(kbps * 1000 / 8 * seconds)


def test_kbps_known_duration_min_and_max():
    p = _profile(FLAC_MP3, "FLAC 16bit", definitions=DEFS)
    dur = DurationInfo(2400)
    ok = _score(p, "A - B [MP3 320]", size_bytes=_size(320, 2400), duration=dur)
    assert ok.is_acceptable and ok.breakdown.kbps["measured"] == pytest.approx(320, abs=1)
    assert ok.kbps_distance == pytest.approx(0, abs=1)
    low = _score(p, "A - B [MP3 320]", size_bytes=_size(128, 2400), duration=dur)
    assert low.breakdown.rejections[0]["code"] == "kbps_below_min"
    high = _score(p, "A - B [MP3 320]", size_bytes=_size(900, 2400), duration=dur)
    assert high.breakdown.rejections[0]["code"] == "kbps_above_max"


def test_kbps_estimated_duration_only_enforces_max():
    p = _profile(FLAC_MP3, "FLAC 16bit", definitions=DEFS)
    est = DurationInfo(2400, estimated=True, source="estimate")
    assert _score(p, "A - B [MP3 320]", size_bytes=_size(128, 2400), duration=est).is_acceptable is True
    r = _score(p, "A - B [MP3 320]", size_bytes=_size(900, 2400), duration=est)
    assert r.is_acceptable is False and "estimated duration" in r.rejection_reasons[0]
    assert r.breakdown.kbps["estimated"] is True


def test_kbps_unknown_duration_or_size_never_rejects():
    p = _profile(FLAC_MP3, "FLAC 16bit", definitions=DEFS)
    r = _score(p, "A - B [MP3 320]", size_bytes=1, duration=None)
    assert r.is_acceptable and r.breakdown.kbps["skipped_reason"] == "duration unknown"
    r = _score(p, "A - B [MP3 320]", size_bytes=None, duration=DurationInfo(100))
    assert r.is_acceptable and r.breakdown.kbps["skipped_reason"] == "release size unknown"


def test_resolve_durations_known_partial_estimate_unknown():
    assert resolve_durations([100, 200], 2) == DurationInfo(300, False, "tracks")
    partial = resolve_durations([100, None], 4)
    assert partial.estimated and partial.seconds == pytest.approx(400)
    est = resolve_durations([None, None], 10)
    assert est.estimated and est.seconds == 2400
    assert resolve_durations([], None) is None


# ------------------------------------------------------------------ ranking and score


def _cand(title, protocol="torrent", seeders=0, size=0, idx=0):
    return AcquisitionSearchResult(download_id=f"id{idx}", title=title, artist="A", protocol=protocol,
                                   seeders=seeders, size_bytes=size, source=protocol)


def test_quality_beats_format_score():
    fmt = _fmt(_spec("ReleaseTitleSpecification", r"\bcd\b"))
    p = _profile(FLAC_MP3, "FLAC 16bit", [fmt], scores={1: 5000})
    ranked = AcquisitionCoordinator().evaluate_and_rank(
        [_cand("A - B [MP3 320] CD", idx=1), _cand("A - B [FLAC 16bit]", idx=2)], p
    )
    assert [c.download_id for c, _ in ranked] == ["id2", "id1"]
    assert ranked[0][1].score > ranked[1][1].score


def test_ranking_order_format_then_kbps_then_protocol_then_seeders(db):
    fmt = _fmt(_spec("ReleaseTitleSpecification", r"\bcd\b"))
    p = _profile(FLAC_MP3, "FLAC 16bit", [fmt], scores={1: 10}, definitions=DEFS)
    coord = AcquisitionCoordinator()

    def order(cands):
        return [c.download_id for c, _ in coord.evaluate_and_rank(cands, p)]

    # format score first
    assert order([_cand("A - B [FLAC 16bit]", idx=1), _cand("A - B [FLAC 16bit] CD", idx=2)]) == ["id2", "id1"]
    # then protocol (usenet > torrent > soulseek), seeders irrelevant across protocols
    assert PROTOCOL_PREFERENCE == ("usenet", "torrent", "soulseek")
    assert order([
        _cand("A - B [FLAC 16bit]", "soulseek", idx=1),
        _cand("A - B [FLAC 16bit]", "torrent", seeders=500, idx=2),
        _cand("A - B [FLAC 16bit]", "usenet", idx=3),
    ]) == ["id3", "id2", "id1"]
    # then seeders for equal everything else
    assert order([_cand("A - B [FLAC 16bit]", seeders=1, idx=1), _cand("A - B [FLAC 16bit]", seeders=9, idx=2)]) == ["id2", "id1"]
    assert protocol_rank("slskd") == 2 and protocol_rank("newznab") == 0


def test_kbps_distance_breaks_ties_before_protocol():
    p = _profile(FLAC_MP3, "FLAC 16bit", definitions=DEFS)
    a = _score(p, "A - B [MP3 320]", size_bytes=_size(320, 2400), duration=DurationInfo(2400))
    b = _score(p, "A - B [MP3 320]", size_bytes=_size(345, 2400), duration=DurationInfo(2400))
    assert rank_key("torrent", 0, a) > rank_key("usenet", 0, b)


def _album_with_tracks(db, seconds):
    artist = db.upsert_library_artist({"name": "A"})
    album = db.upsert_library_album({"artist_id": artist["id"], "title": "B", "total_tracks": len(seconds)})
    for n, sec in enumerate(seconds, start=1):
        db.upsert_library_track(
            {"album_id": album["id"], "artist_id": artist["id"], "title": f"T{n}", "track_number": n,
             "duration_seconds": sec}
        )
    return album["id"]


def test_evaluate_and_rank_applies_kbps_with_album_duration_from_db(db):
    album_id = _album_with_tracks(db, [600.0, 600.0, 600.0, 600.0])  # 2400 s known
    prof = db.get_quality_profile("profile-high-quality")
    cands = [
        _cand("A - B [MP3 320]", size=_size(100, 2400), idx=1),  # 100 kbps < 290 min
        _cand("A - B [MP3 320]", size=_size(320, 2400), idx=2),
    ]
    coord = AcquisitionCoordinator()
    with_album = coord.evaluate_and_rank(cands, prof, db=db, album_id=album_id, item_type="album")
    assert [c.download_id for c, _ in with_album] == ["id2"]
    assert len(coord.evaluate_and_rank(cands, prof, db=db)) == 2  # no album context: no kbps rejection


def test_estimated_album_duration_enforces_max_only(db):
    artist = db.upsert_library_artist({"name": "A"})
    album = db.upsert_library_album({"artist_id": artist["id"], "title": "B", "total_tracks": 10})  # no tracks
    prof = db.get_quality_profile("profile-high-quality")
    cands = [_cand("A - B [MP3 320]", size=_size(100, 2400), idx=1), _cand("A - B [MP3 320]", size=_size(900, 2400), idx=2)]
    ranked = AcquisitionCoordinator().evaluate_and_rank(cands, prof, db=db, album_id=album["id"], item_type="album")
    assert [c.download_id for c, _ in ranked] == ["id1"]


# ------------------------------------------------------------------ upgrades


def test_upgrade_rules():
    p = _profile(FLAC_MP3, "FLAC 16bit", [_fmt(_spec("ReleaseTitleSpecification", r"\bcd\b"))], scores={1: 10},
                 min_upgrade_format_score=5, cutoff_format_score=0)
    cur_mp3 = _score(p, "A - B [MP3 320]")
    new_flac = _score(p, "A - B [FLAC 16bit]")
    assert evaluate_upgrade(cur_mp3, new_flac, p).is_upgrade  # higher quality
    assert not evaluate_upgrade(new_flac, cur_mp3, p).is_upgrade  # cutoff met / lower quality
    # same quality below cutoff: needs +5
    cur_320 = _score(p, "A - B [MP3 320]")
    better = _score(p, "A - B [MP3 320] CD")
    d = evaluate_upgrade(cur_320, better, p)
    assert d.is_upgrade and "10" in d.reason
    p.min_upgrade_format_score = 11
    assert not evaluate_upgrade(cur_320, better, p).is_upgrade
    # floor: same-tier candidate must exceed current + (min_upgrade - 1)
    assert upgrade_floor(cur_320, p) == cur_320.score + 10
    assert better.score <= upgrade_floor(cur_320, p)
    assert new_flac.score > upgrade_floor(cur_320, p)  # a better quality always clears the floor
    p.upgrade_allowed = False
    assert not evaluate_upgrade(cur_320, new_flac, p).is_upgrade


def test_cutoff_format_score_keeps_item_wanted():
    p = _profile(FLAC_MP3, "FLAC 16bit", [_fmt(_spec("ReleaseTitleSpecification", r"\bcd\b"))], scores={1: 10},
                 cutoff_format_score=10)
    assert _score(p, "A - B [FLAC 16bit]").meets_cutoff is False  # quality ok, score below upgrade-until
    assert _score(p, "A - B [FLAC 16bit] CD").meets_cutoff is True


def test_min_format_score_rejects():
    p = _profile(FLAC_MP3, "FLAC 16bit", [_fmt(_spec("ReleaseTitleSpecification", r"vinyl"))], scores={1: -50},
                 min_format_score=0)
    r = _score(p, "A - B [FLAC] vinyl")
    assert not r.is_acceptable and r.breakdown.rejections[0]["code"] == "format_score_below_min"


def test_groups_share_a_tier():
    entries = [{"type": "group", "name": "Lossless", "allowed": True, "items": ["FLAC 24bit", "FLAC 16bit"]}, _q("MP3 320")]
    p = _profile(entries, "Lossless")
    a, b = _score(p, "A - B [FLAC 24bit]"), _score(p, "A - B [FLAC 16bit]")
    assert a.tier == b.tier == 0 and a.meets_cutoff and b.meets_cutoff
    assert a.breakdown.tier_name == "Lossless"
    assert _score(p, "A - B [MP3 320]").meets_cutoff is False


# ------------------------------------------------------------------ regex safety


@pytest.mark.parametrize("pattern", [r"(a+)+$", r"(\w*\s?)*", r"(a|aa)+", r"(.*a){20}x*(b+)*", "x" * 501, "(unclosed"])
def test_unsafe_patterns_rejected(pattern):
    with pytest.raises(UnsafeRegexError):
        validate_pattern(pattern)


@pytest.mark.parametrize("pattern", [r"\b(?:FLAC|Lossless)\b", r"24.?bit|Hi.?Res", r"web(?:-?dl|-?rip)?", r"(?<g>ab)c", r"(foo|bar)+"])
def test_safe_patterns_accepted(pattern):
    validate_pattern(pattern)


def test_catastrophic_pattern_never_runs_in_engine():
    # Stored directly (bypassing API validation): the engine refuses to run it rather than hang.
    p = _profile(FLAC_MP3, "FLAC 16bit", [_fmt(_spec("ReleaseTitleSpecification", r"(a+)+$"))], scores={1: 9})
    start = time.monotonic()
    r = _score(p, "A - B " + "a" * 40 + "!")
    assert time.monotonic() - start < 1
    assert r.format_score == 0 and r.breakdown.notes


def test_budget_skips_remaining_regexes():
    b = Budget(0)
    time.sleep(0.001)
    assert safe_search("a", "a", b) is None and b.exhausted


def test_input_is_truncated():
    assert safe_search("needle$", "x" * 5000 + "needle") is False


# ------------------------------------------------------------------ breakdown


def test_breakdown_output_shape():
    p = _profile(
        FLAC_MP3, "FLAC 16bit",
        [_fmt(_spec("ReleaseTitleSpecification", r"\bcd\b"), name="CD")],
        release_profiles=[_rp(ignored=["bootleg"])], definitions=DEFS, scores={1: 10},
    )
    r = _score(p, "A - B [FLAC 16bit] CD bootleg", size_bytes=_size(900, 2400), duration=DurationInfo(2400),
               protocol="newznab")
    d = r.to_dict()["breakdown"]
    assert d["quality"] == "FLAC 16bit" and d["tier"] == 1 and d["quality_allowed"] is True
    assert d["protocol"] == "usenet"
    assert d["matched_formats"] == [{"id": 1, "name": "CD", "score": 10}]
    assert d["format_score"] == 10 and d["total_score"] == r.score
    assert d["kbps"]["checked"] and d["kbps"]["estimated"] is False and d["kbps"]["preferred"] == 895.0
    assert [x["code"] for x in d["rejections"]] == ["release_profile_ignored"]
    assert d["release_profiles"][0]["result"] == "rejected"
