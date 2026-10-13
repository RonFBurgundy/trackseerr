"""AcoustID fingerprint lookup + rename-verification engine (acoustid fully mocked)."""

import os
import threading
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

import trackseerr.library as library_mod
import trackseerr.rename_verification as rv
from trackseerr.storage import Database

R1, R2, R3 = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222", "33333333-3333-3333-3333-333333333333"


def _fake_acoustid(results: list[tuple[float, str, str, str]] | None = None) -> MagicMock:
    fake = MagicMock()
    fake.FingerprintGenerationError = type("FGE", (Exception,), {})
    fake.NoBackendError = type("NBE", (fake.FingerprintGenerationError,), {})
    fake.WebServiceError = type("WSE", (Exception,), {})
    fake.fingerprint_file.return_value = (123.0, b"AQAA-fp")
    fake.lookup.return_value = {"results": []}
    fake.parse_lookup_result.side_effect = lambda resp: iter(results or [])
    return fake


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(library_mod, "_acoustid_rate_limit", lambda: None)


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    with rv._state_lock:
        rv._state.clear()
        rv._state.update(rv._fresh_state())
    yield


def _run_lookup(fake: MagicMock, path: Path) -> dict[str, Any]:
    with patch.dict("sys.modules", {"acoustid": fake}):
        return library_mod.fingerprint_and_lookup(path, "key")


# ------------------------------------------------------------------ fingerprint_and_lookup


def test_fingerprint_and_lookup_filters_and_dedupes_matches(tmp_path: Path) -> None:
    fake = _fake_acoustid([(0.95, R1, "One", "A"), (0.90, R1, "One b", "A"), (0.85, R2, "Two", "B"), (0.5, R3, "Three", "C")])
    res = _run_lookup(fake, tmp_path / "a.flac")
    assert res["status"] == "ok"
    assert res["fingerprint"] == "AQAA-fp"
    assert res["duration"] == 123.0
    assert res["matches"] == [
        {"recording_id": R1, "title": "One", "artist": "A", "score": 0.95},
        {"recording_id": R2, "title": "Two", "artist": "B", "score": 0.85},
    ]
    fake.fingerprint_file.assert_called_once_with(str(tmp_path / "a.flac"), force_fpcalc=True)


def test_fingerprint_and_lookup_keeps_best_score_when_lower_comes_first(tmp_path: Path) -> None:
    res = _run_lookup(_fake_acoustid([(0.82, R1, "Low", "A"), (0.97, R1, "High", "A")]), tmp_path / "a.flac")
    assert res["matches"] == [{"recording_id": R1, "title": "High", "artist": "A", "score": 0.97}]


def test_fingerprint_and_lookup_no_result(tmp_path: Path) -> None:
    res = _run_lookup(_fake_acoustid([(0.3, R1, "x", "y")]), tmp_path / "a.flac")
    assert res["status"] == "no_result"
    assert res["matches"] == []
    assert res["fingerprint"] == "AQAA-fp"


def test_fingerprint_and_lookup_webservice_error_keeps_fingerprint(tmp_path: Path) -> None:
    fake = _fake_acoustid()
    fake.lookup.side_effect = fake.WebServiceError("boom")
    res = _run_lookup(fake, tmp_path / "a.flac")
    assert res["status"] == "error"
    assert res["fingerprint"] == "AQAA-fp"
    assert res["duration"] == 123.0
    assert "boom" in res["error"]


def test_fingerprint_and_lookup_generation_error(tmp_path: Path) -> None:
    fake = _fake_acoustid()
    fake.fingerprint_file.side_effect = fake.FingerprintGenerationError("bad audio")
    res = _run_lookup(fake, tmp_path / "a.flac")
    assert res == {"status": "error", "error": "bad audio", "duration": None, "fingerprint": None, "matches": []}
    fake.lookup.assert_not_called()


def test_fingerprint_and_lookup_no_backend_raises_unavailable(tmp_path: Path) -> None:
    fake = _fake_acoustid()
    fake.fingerprint_file.side_effect = fake.NoBackendError("no fpcalc")
    with pytest.raises(library_mod.FingerprintUnavailable):
        _run_lookup(fake, tmp_path / "a.flac")


# ------------------------------------------------------------------ verdict_for


def _fp(path: str = "/m/a.flac", status: str = "ok", matches: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"file_path": path, "status": status, "matches": matches if matches is not None else []}


def _m(rid: str, score: float = 0.9, title: str = "T", artist: str = "Art") -> dict[str, Any]:
    return {"recording_id": rid, "title": title, "artist": artist, "score": score}


@pytest.fixture
def lib(tmp_path: Path) -> Iterator[Database]:
    db = Database(str(tmp_path / "verify.db"))
    db.upsert_library_artist({"id": "ar", "name": "Artist", "monitored": True})
    for alb in ("al1", "al2"):
        db.upsert_library_album({"id": alb, "artist_id": "ar", "title": alb, "year": 2000, "monitored": True})
    yield db
    db.close()


def _track(db: Database, tid: str, album: str, rec: str | None, title: str = "Song") -> dict[str, Any]:
    return db.upsert_library_track(
        {"id": tid, "album_id": album, "artist_id": "ar", "title": title, "track_number": 1, "mb_recording_id": rec}
    )


def test_verdict_verified_when_track_recording_in_matches(lib: Database) -> None:
    trk = _track(lib, "t1", "al1", R1)
    v = rv.verdict_for(lib, _fp(matches=[_m(R2, 0.95), _m(R1, 0.88)]), trk, "/m/a.flac")
    assert v is not None and v["status"] == rv.VERIFIED and v["score"] == 0.88
    assert v["actual_track_id"] is None


def test_verdict_mismatch_links_actual_library_track_same_album_first(lib: Database) -> None:
    trk = _track(lib, "t1", "al1", R1, "Linked")
    _track(lib, "other-album", "al2", R2, "Elsewhere")
    _track(lib, "same-album", "al1", R2, "Right One")
    v = rv.verdict_for(lib, _fp(matches=[_m(R2, 0.93)]), trk, "/m/a.flac")
    assert v is not None
    assert v["status"] == rv.MISMATCH
    assert v["actual_track_id"] == "same-album"
    assert v["actual_title"] == "Right One"
    assert v["actual_album_id"] == "al1"
    assert v["score"] == 0.93


def test_verdict_mismatch_falls_back_to_other_album(lib: Database) -> None:
    trk = _track(lib, "t1", "al1", R1)
    _track(lib, "elsewhere", "al2", R2, "Elsewhere")
    v = rv.verdict_for(lib, _fp(matches=[_m(R2)]), trk, "/m/a.flac")
    assert v is not None and v["actual_track_id"] == "elsewhere" and v["actual_album_id"] == "al2"


def test_verdict_mismatch_without_library_track_uses_acoustid_title(lib: Database) -> None:
    trk = _track(lib, "t1", "al1", R1)
    v = rv.verdict_for(lib, _fp(matches=[_m(R2, 0.91, "Real Title", "Real Artist")]), trk, "/m/a.flac")
    assert v == {
        "status": rv.MISMATCH, "score": 0.91, "actual_track_id": None, "actual_album_id": None,
        "actual_title": "Real Title", "actual_artist": "Real Artist",
    }


def test_verdict_unknown_without_target_recording_or_result(lib: Database) -> None:
    no_rec = _track(lib, "t1", "al1", None)
    v = rv.verdict_for(lib, _fp(matches=[_m(R2)]), no_rec, "/m/a.flac")
    assert v is not None and v["status"] == rv.UNKNOWN and v["score"] is None
    with_rec = _track(lib, "t2", "al1", R1)
    for status in ("no_result", "error"):
        v2 = rv.verdict_for(lib, _fp(status=status), with_rec, "/m/a.flac")
        assert v2 is not None and v2["status"] == rv.UNKNOWN


def test_verdict_verified_when_hit_is_the_linked_track(lib: Database) -> None:
    trk = _track(lib, "t1", "al1", None)
    # linked track has no recording id of its own, but the matched id resolves to this very track
    lib.conn.execute("UPDATE library_tracks SET mb_recording_id = ? WHERE id = 't1'", (R2,))
    lib.conn.commit()
    stale_view = {**trk, "mb_recording_id": None}
    v = rv.verdict_for(lib, _fp(matches=[_m(R2, 0.9)]), stale_view, "/m/a.flac")
    assert v is not None and v["status"] == rv.VERIFIED


def test_verdict_none_when_cache_path_stale(lib: Database) -> None:
    trk = _track(lib, "t1", "al1", R1)
    assert rv.verdict_for(lib, None, trk, "/m/a.flac") is None
    assert rv.verdict_for(lib, _fp(path="/old/a.flac", matches=[_m(R1)]), trk, "/m/a.flac") is None


def test_is_risky() -> None:
    assert rv.is_risky(["track_number"]) is True
    assert rv.is_risky(["artist_folder", "file_name_format"]) is False


# ------------------------------------------------------------------ verify_file / run_verification


def _file(lib: Database, tmp_path: Path, fid: str = "f1", tid: str = "t1", rec: str | None = R1) -> dict[str, Any]:
    if lib.get_library_track(tid) is None:
        _track(lib, tid, "al1", rec)
    audio = tmp_path / f"{fid}.flac"
    audio.write_bytes(b"audio" + fid.encode())
    return lib.upsert_library_file(
        {"id": fid, "track_id": tid, "file_path": str(audio), "relative_path": audio.name}
    )


def test_verify_file_cache_hit_skips_network(lib: Database, tmp_path: Path) -> None:
    f = _file(lib, tmp_path)
    fake = _fake_acoustid([(0.9, R1, "T", "A")])
    with patch.dict("sys.modules", {"acoustid": fake}):
        first = rv.verify_file(lib, f, "k")
        second = rv.verify_file(lib, f, "k")
    assert fake.lookup.call_count == 1
    assert first["cached"] is False and second["cached"] is True
    assert second["status"] == "ok" and second["matches"][0]["recording_id"] == R1


def test_verify_file_rescans_when_mtime_changes(lib: Database, tmp_path: Path) -> None:
    f = _file(lib, tmp_path)
    fake = _fake_acoustid([(0.9, R1, "T", "A")])
    with patch.dict("sys.modules", {"acoustid": fake}):
        rv.verify_file(lib, f, "k")
        st = os.stat(f["file_path"])
        os.utime(f["file_path"], ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        again = rv.verify_file(lib, f, "k")
    assert fake.lookup.call_count == 2
    assert again["cached"] is False


def test_verify_file_retries_error_rows(lib: Database, tmp_path: Path) -> None:
    f = _file(lib, tmp_path)
    fake = _fake_acoustid([(0.9, R1, "T", "A")])
    fake.lookup.side_effect = [fake.WebServiceError("down"), {"results": []}]
    with patch.dict("sys.modules", {"acoustid": fake}):
        first = rv.verify_file(lib, f, "k")
        second = rv.verify_file(lib, f, "k")
    assert first["status"] == "error"
    assert second["status"] == "ok" and second["cached"] is False
    assert fake.lookup.call_count == 2


def test_verify_file_missing_file_not_cached(lib: Database, tmp_path: Path) -> None:
    f = _file(lib, tmp_path)
    os.remove(f["file_path"])
    assert rv.verify_file(lib, f, "k")["error"] == "file missing"
    assert lib.get_file_fingerprint("f1") is None


def test_run_verification_counts_and_stops_on_unavailable(lib: Database, tmp_path: Path) -> None:
    ok = _file(lib, tmp_path, "f1", "t1", R1)
    bad = _file(lib, tmp_path, "f2", "t2", R3)
    fake = _fake_acoustid([(0.9, R1, "T", "A")])
    with patch.dict("sys.modules", {"acoustid": fake}):
        counts = rv.run_verification(lib, [ok["id"], bad["id"], "nope"], "k")
    assert counts["running"] is False and counts["finished_at"] is not None
    assert (counts["verified"], counts["mismatched"], counts["errors"], counts["done"], counts["total"]) == (1, 1, 1, 3, 3)

    third = _file(lib, tmp_path, "f3", "t3", R1)
    unavailable = _fake_acoustid()
    unavailable.fingerprint_file.side_effect = unavailable.NoBackendError("none")
    with patch.dict("sys.modules", {"acoustid": unavailable}):
        out = rv.run_verification(lib, [third["id"], ok["id"]], "k")
    assert out["message"] == rv.UNAVAILABLE_MESSAGE
    assert out["running"] is False and out["done"] == 0


def test_run_verification_honours_should_stop(lib: Database, tmp_path: Path) -> None:
    f = _file(lib, tmp_path)
    out = rv.run_verification(lib, [f["id"]], "k", should_stop=lambda: True)
    assert out["done"] == 0 and out["running"] is False


def test_start_verification_refuses_second_run(lib: Database) -> None:
    release = threading.Event()
    started = threading.Event()

    def blocking(*args: Any, **kwargs: Any) -> dict[str, Any]:
        started.set()
        release.wait(5)
        return rv.get_verification_status()

    with patch.object(rv, "run_verification", blocking):
        assert rv.start_verification(lib, ["f1"], "k") is True
        assert started.wait(5)
        assert rv.get_verification_status()["running"] is True
        assert rv.start_verification(lib, ["f1"], "k") is False
        release.set()
        for t in threading.enumerate():
            if t.name == "rename-verify":
                t.join(5)
