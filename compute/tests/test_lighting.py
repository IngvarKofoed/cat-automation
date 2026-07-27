"""The day/night lighting flag: analyzer, read-time threshold, histogram, regime scoping.

See docs/specs/2026-07-27-lighting-flag.md. The statistic itself is tested in
``shared/tests/test_motion.py``; these tests pin the compute-side contract around
it — the parts that make "sweep now, calibrate later" actually hold:

- ``LightingAnalyzer`` is NON-registered (absent from ``ANALYZER_NAMES``), so it can
  never be offered as gate ground truth in the scorecard / disagreement / oracle
  paths, and its row carries the STATISTIC in ``score`` rather than a baked verdict.
- ``resolve_lighting`` is the one read-time rule, including the two states that must
  never collapse: uncalibrated-but-measured (reads ``day``, flagged) versus unswept
  (reads ``None``).
- The threshold round-trips through the settings KV and degrades a corrupt or
  non-finite stored value to "uncalibrated" rather than to a wrong cutoff.
- ``gate_scorecard(regime=...)`` is an EXACT partition: day + night equals the
  unscoped card, for both frame metrics and visit counts (which use different but
  each-internally-correct assignment rules).
"""
from __future__ import annotations

import pytest

from compute.analysis import ANALYZER_NAMES
from compute.analysis.lighting import LIGHTING_ANALYZER, LightingAnalyzer
from compute.collection.store import Store
from compute.ingest.client import StreamFrame, StreamFrameMeta

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

_DAY_TS = 1_700_000_000_000
_NIGHT_TS = _DAY_TS + 12 * 3_600_000


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=50_000_000,
    )


def _push(store: Store, img, recv_ts: int, motion: bool = False, area: float = 0.02) -> int:
    jpeg = cv2.imencode(".jpg", img)[1].tobytes()
    meta = StreamFrameMeta(frame_id=recv_ts % 100_000, ts=0, motion=motion, bbox=None, area=area)
    return store.add(StreamFrame(meta, jpeg), recv_ts_ms=recv_ts)


def _colour():
    img = np.zeros((60, 80, 3), np.uint8)
    img[:, :40] = (40, 170, 60)
    img[:, 40:] = (170, 60, 40)
    return img


def _mono():
    img = np.full((60, 80, 3), 130, np.uint8)
    img[:, :40] = 100
    return img


# --- The analyzer ------------------------------------------------------------


def test_lighting_analyzer_is_not_in_the_oracle_registry():
    # It is not ground truth about cats or motion, so registering it would wrongly
    # offer it in the scorecard / disagreement / oracle-coverage paths. Same
    # deliberate exclusion CorruptionAnalyzer has.
    assert LIGHTING_ANALYZER not in ANALYZER_NAMES
    assert LightingAnalyzer().name == LIGHTING_ANALYZER
    assert LightingAnalyzer().windowed is False


def test_lighting_analyzer_records_the_statistic_not_a_verdict():
    a = LightingAnalyzer()
    a.ensure_available()          # no-op: cv2/numpy are base deps, never ML extras
    a.prepare(store=None)         # no-op: stateless, nothing to prime
    colour, mono = a.analyze(_colour()), a.analyze(_mono())

    # `verdict` is always False and no reader consults it — the row exists to carry
    # `score`, which is where the continuous statistic lives.
    assert colour.verdict is False and mono.verdict is False
    assert colour.score > mono.score
    assert mono.score == pytest.approx(0.0, abs=1e-6)
    for r in (colour, mono):
        assert "luma" in r.detail            # the dark-frame ambiguity stays recoverable
        assert isinstance(r.detail["version"], int)   # staleness stamp


def test_lighting_job_gets_its_own_queue_category():
    # The tuning UI renders one queue panel per category; without its own bucket a
    # lighting sweep would appear under the YOLO card's table.
    from compute.analysis.runner import _job_category

    assert _job_category(LIGHTING_ANALYZER) == "lighting"
    assert _job_category("mog2:candidate") == "mog2"
    assert _job_category("yolo-serial") == "coverage"


# --- The read-time threshold -------------------------------------------------


@pytest.mark.parametrize(
    "score,threshold,expected",
    [
        (None, None, (None, False)),      # unswept: no statistic at all
        (None, 0.5, (None, False)),       # unswept stays unswept even WITH a cutoff
        (0.9, None, ("day", False)),      # measured but uncalibrated -> assumed day
        (0.0, None, ("day", False)),      # ...even at zero: the cutoff is what decides
        (0.1, 0.5, ("night", True)),
        (0.5, 0.5, ("day", True)),        # the boundary is inclusive on the day side
        (0.9, 0.5, ("day", True)),
    ],
)
def test_resolve_lighting_table(score, threshold, expected):
    assert Store.resolve_lighting(score, threshold) == expected


def test_unswept_and_uncalibrated_are_distinct_states():
    # The rule the corruption page had to learn: a sweep that never ran must not
    # present as a measurement. Both resolve falsy-ish, but only one has a label.
    unswept, _ = Store.resolve_lighting(None, None)
    uncalibrated, calibrated = Store.resolve_lighting(0.42, None)
    assert unswept is None
    assert (uncalibrated, calibrated) == ("day", False)


def test_threshold_round_trips_and_clears(tmp_path):
    store = _store(tmp_path)
    try:
        assert store.get_lighting_threshold() is None
        store.set_lighting_threshold(0.25)
        assert store.get_lighting_threshold() == pytest.approx(0.25)
        # Clearing is a real operation, not an absence: a pre-NoIR cutoff is wrong
        # for the new sensor, and clearing reads back as uncalibrated.
        store.set_lighting_threshold(None)
        assert store.get_lighting_threshold() is None
    finally:
        store.close()


def test_threshold_endpoint_requires_an_explicit_value(tmp_path):
    """`{}` must NOT clear a calibrated cutoff. The field is required-but-nullable, so
    clearing is always deliberate — an empty body (a retried request whose body was
    stripped) would otherwise wipe the calibration with a 200."""
    from pydantic import ValidationError

    from compute.api.app import LightingThresholdRequest

    with pytest.raises(ValidationError):
        LightingThresholdRequest()
    # A bool must not coerce: pydantic treats bool as an int subtype, so `true` would
    # otherwise become a 1.0 cutoff — above every real value, i.e. everything reads night.
    with pytest.raises(ValidationError):
        LightingThresholdRequest(threshold=True)
    with pytest.raises(ValidationError):
        LightingThresholdRequest(threshold=-0.5)
    # Explicit null is the CLEAR operation and stays valid.
    assert LightingThresholdRequest(threshold=None).threshold is None
    assert LightingThresholdRequest(threshold=0.25).threshold == pytest.approx(0.25)


@pytest.mark.parametrize("raw", ["", "abc", "nan", "inf", "-inf"])
def test_corrupt_or_non_finite_threshold_degrades_to_uncalibrated(tmp_path, raw):
    # A non-finite cutoff would make every comparison below it uniformly true or
    # false, so it must read exactly like "never configured" (get_location's rule).
    store = _store(tmp_path)
    try:
        store.set_setting("lighting_threshold", raw)
        assert store.get_lighting_threshold() is None
    finally:
        store.close()


def test_sample_frames_flags_attach_lighting(tmp_path):
    store = _store(tmp_path)
    try:
        ids = [_push(store, _colour(), _DAY_TS), _push(store, _mono(), _DAY_TS + 200)]
        # Unswept: a lighting key is present but carries no reading.
        for f in store.sample_frames(None, None, 10, flags=True):
            assert f["lighting"] is None and f["colourfulness"] is None

        a = LightingAnalyzer()
        store.write_analysis_batch([
            (ids[0], LIGHTING_ANALYZER, False, a.analyze(_colour()).score, None),
            (ids[1], LIGHTING_ANALYZER, False, a.analyze(_mono()).score, None),
        ])
        by_id = {f["id"]: f for f in store.sample_frames(None, None, 10, flags=True)}
        # Measured but uncalibrated -> both read day, both flagged.
        assert [by_id[i]["lighting"] for i in ids] == ["day", "day"]
        assert all(by_id[i]["lighting_calibrated"] is False for i in ids)

        store.set_lighting_threshold(0.2)
        by_id = {f["id"]: f for f in store.sample_frames(None, None, 10, flags=True)}
        assert by_id[ids[0]]["lighting"] == "day"
        assert by_id[ids[1]]["lighting"] == "night"
        assert all(by_id[i]["lighting_calibrated"] is True for i in ids)
    finally:
        store.close()


def test_sample_frames_without_flags_has_no_lighting_keys(tmp_path):
    # The additive contract: absent `flags` the payload is byte-identical.
    store = _store(tmp_path)
    try:
        _push(store, _colour(), _DAY_TS)
        row = store.sample_frames(None, None, 10)[0]
        assert set(row) == {"id", "recv_ts", "url"}
    finally:
        store.close()


# --- The histogram (what a threshold gets picked FROM) -----------------------


def test_histogram_is_empty_when_unswept(tmp_path):
    store = _store(tmp_path)
    try:
        _push(store, _colour(), _DAY_TS)
        h = store.lighting_histogram()
        assert h == {"count": 0, "min": None, "max": None, "buckets": []}
    finally:
        store.close()


def test_histogram_buckets_sum_to_the_measured_count(tmp_path):
    store = _store(tmp_path)
    try:
        a = LightingAnalyzer()
        rows = []
        for i in range(20):
            img = _colour() if i % 2 else _mono()
            fid = _push(store, img, _DAY_TS + i * 200)
            rows.append((fid, LIGHTING_ANALYZER, False, a.analyze(img).score, None))
        store.write_analysis_batch(rows)

        h = store.lighting_histogram()
        assert h["count"] == 20
        assert sum(b["n"] for b in h["buckets"]) == 20
        assert h["min"] < h["max"]
        # Bimodal by construction: the extreme buckets hold everything.
        assert h["buckets"][0]["n"] == 10 and h["buckets"][-1]["n"] > 0
    finally:
        store.close()


def test_histogram_collapses_a_degenerate_range_to_one_bucket(tmp_path):
    # Every frame measured the same value: one bucket is the honest answer, and it
    # must not divide by a zero-width range.
    store = _store(tmp_path)
    try:
        rows = []
        for i in range(5):
            fid = _push(store, _mono(), _DAY_TS + i * 200)
            rows.append((fid, LIGHTING_ANALYZER, False, 0.0, None))
        store.write_analysis_batch(rows)
        h = store.lighting_histogram()
        assert h["count"] == 5 and len(h["buckets"]) == 1
        assert h["buckets"][0]["n"] == 5
    finally:
        store.close()


# --- Regime scoping of the scorecard ----------------------------------------


def _scorecard_fixture(store: Store) -> None:
    """8 oracle-present visits, 4 per half-day, alternating caught / wholly missed."""
    ids = []
    for v in range(8):
        base = (_DAY_TS if v < 4 else _NIGHT_TS) + v * 600_000
        for k in range(3):
            ids.append(_push(store, _mono(), base + k * 200, motion=(v % 2 == 0)))
    store.write_analysis_batch([(i, "yolo-serial", True, 0.9, None) for i in ids])


_SC_KW = dict(warmup=0, min_area=0.01, max_area=0.6, persistence=2)


def test_regime_scoping_is_an_exact_partition(tmp_path):
    store = _store(tmp_path)
    try:
        _scorecard_fixture(store)
        is_night = lambda ts: ts >= _NIGHT_TS  # noqa: E731
        kw = dict(_SC_KW, is_night=is_night)
        both = store.gate_scorecard("live", "yolo-serial", **kw)
        day = store.gate_scorecard("live", "yolo-serial", regime="day", **kw)
        night = store.gate_scorecard("live", "yolo-serial", regime="night", **kw)

        # Frame metrics partition (each frame assigned by its OWN timestamp).
        assert both["present"] == day["present"] + night["present"]
        for k in ("caught", "missed"):
            assert both["recall"][k] == day["recall"][k] + night["recall"][k]
        assert (both["false_triggers"]["count"]
                == day["false_triggers"]["count"] + night["false_triggers"]["count"])
        # Visit counts partition (each visit assigned WHOLE by its first present
        # frame), and match the split the unscoped card already reports — so a
        # dusk-straddling visit is never cut in half by the scoping.
        for k in ("total", "caught", "wholly_missed"):
            assert both["visits"][k] == day["visits"][k] + night["visits"][k]
        assert both["split"]["day"]["total"] == day["visits"]["total"]
        assert both["split"]["night"]["total"] == night["visits"]["total"]
        # The card says it is scoped, so a reader can't mistake it for the whole day.
        assert day["regime"] == "day" and night["regime"] == "night"
        assert "regime" not in both
        # And it does NOT also carry the unscoped split: day + night of that would
        # contradict the scoped `visits` sitting beside it under a "Day only" heading.
        assert "split" not in day and "split" not in night
    finally:
        store.close()


def test_lighting_staleness_counts_rows_from_an_older_formula(tmp_path):
    # The version stamp is only worth writing if something reads it: after a bump the
    # histogram would otherwise silently superimpose two incompatible distributions.
    from shared.motion import lighting_version

    store = _store(tmp_path)
    try:
        cur, old = _push(store, _mono(), _DAY_TS), _push(store, _mono(), _DAY_TS + 200)
        store.write_analysis_batch([
            (cur, LIGHTING_ANALYZER, False, 0.1, {"luma": 1.0, "version": lighting_version()}),
            (old, LIGHTING_ANALYZER, False, 0.1, {"luma": 1.0, "version": lighting_version() - 1}),
        ])
        assert store.lighting_staleness() == {"analyzed": 2, "stale": 1}
    finally:
        store.close()


def test_scoped_counts_match_the_sql_aggregate_exactly(tmp_path):
    """``_count_interesting`` is a PYTHON twin of the scorecard's ``SUM(CASE ...)``
    aggregate, used only on the scoped path. Give it a classifier that admits every
    frame and it must reproduce the SQL numbers exactly — including SQL's NULL
    semantics, where a comparison against a NULL area/score counts as false rather
    than raising. Without this, the two could drift silently."""
    store = _store(tmp_path)
    try:
        _scorecard_fixture(store)
        # A missed frame with NO oracle score — SQL's confidence CASE arms compare
        # against NULL and count it as neither high nor medium, so it must land in
        # `low`. (A NULL *area* is unreachable for a live source: Store.add requires
        # one. The slot-source test below covers that half.)
        unscored = _push(store, _mono(), _DAY_TS + 5_000_200, motion=False, area=0.02)
        store.write_analysis_batch([(unscored, "yolo-serial", True, None, None)])

        everything_is_day = lambda ts: False  # noqa: E731
        # Run at a POSITIVE oracle_floor, not the 0 default. A NULL score only diverges
        # under a floor: SQL's `verdict = 1 AND score >= floor` is then UNKNOWN, counted
        # on neither side, where a two-valued Python recount would invent a false
        # trigger. At floor 0 the predicate is `verdict = 1` and the bug is invisible —
        # which is exactly how the first version of this test missed it.
        for floor in (0.0, 0.3):
            kw = dict(_SC_KW, is_night=everything_is_day, oracle_floor=floor)
            plain = store.gate_scorecard("live", "yolo-serial", **kw)
            scoped = store.gate_scorecard("live", "yolo-serial", regime="day", **kw)
            assert scoped["present"] == plain["present"], floor
            assert scoped["recall"] == plain["recall"], floor
            assert scoped["false_triggers"] == plain["false_triggers"], floor
            assert scoped["confidence"] == plain["confidence"], floor
            assert scoped["area_buckets"] == plain["area_buckets"], floor
            assert scoped["visits"] == plain["visits"], floor
    finally:
        store.close()


def test_a_null_scored_present_row_counts_on_neither_side(tmp_path):
    """The specific three-valued case, pinned directly rather than only via the
    equivalence test: a positive verdict with a NULL score under a floor is UNKNOWN to
    SQL, so it is neither `present` nor a `false_trigger` — on the scoped path too."""
    store = _store(tmp_path)
    try:
        fid = _push(store, _mono(), _DAY_TS, motion=True, area=0.02)
        store.write_analysis_batch([(fid, "yolo-serial", True, None, None)])
        kw = dict(_SC_KW, oracle_floor=0.3, is_night=lambda ts: False)
        plain = store.gate_scorecard("live", "yolo-serial", **kw)
        scoped = store.gate_scorecard("live", "yolo-serial", regime="day", **kw)
        assert plain["present"] == 0 and plain["false_triggers"]["count"] == 0
        assert scoped["present"] == 0 and scoped["false_triggers"]["count"] == 0
    finally:
        store.close()


def test_scoped_slot_counts_match_the_sql_aggregate_with_null_areas(tmp_path):
    """The other half of the NULL story. For a SLOT source the area is
    ``analysis.score``, which CAN be NULL — SQL puts such a frame in no area bucket
    at all, and Python's ``None < min_area`` would raise. Pin the agreement."""
    store = _store(tmp_path)
    try:
        _scorecard_fixture(store)
        # Mirror the live gate into a slot, but leave two frames' scores NULL.
        rows = []
        for i, (fid, motion) in enumerate(
            store._conn.execute("SELECT id, motion FROM frames ORDER BY id").fetchall()
        ):
            score = None if i < 2 else 0.02
            rows.append((fid, "mog2:candidate", bool(motion), score, None))
        store.write_analysis_batch(rows)

        everything_is_day = lambda ts: False  # noqa: E731
        plain = store.gate_scorecard(
            "mog2:candidate", "yolo-serial", is_night=everything_is_day, **_SC_KW
        )
        scoped = store.gate_scorecard(
            "mog2:candidate", "yolo-serial", regime="day",
            is_night=everything_is_day, **_SC_KW,
        )
        assert not plain.get("needs_rerun")
        assert scoped["area_buckets"] == plain["area_buckets"]
        assert scoped["recall"] == plain["recall"]
        assert scoped["confidence"] == plain["confidence"]
        assert scoped["false_triggers"] == plain["false_triggers"]
    finally:
        store.close()


def test_unscoped_card_is_unchanged_by_the_feature(tmp_path):
    # regime=None must leave the card byte-for-byte what it was, including with no
    # is_night at all (the pre-existing default path).
    store = _store(tmp_path)
    try:
        _scorecard_fixture(store)
        plain = store.gate_scorecard("live", "yolo-serial", **_SC_KW)
        assert "regime" not in plain and "split" not in plain
        assert plain["visits"]["total"] == 8
    finally:
        store.close()


def test_regime_scoped_missed_visits_match_the_scoped_count(tmp_path):
    store = _store(tmp_path)
    try:
        _scorecard_fixture(store)
        is_night = lambda ts: ts >= _NIGHT_TS  # noqa: E731
        kw = dict(_SC_KW, is_night=is_night, missed_visits=True)
        for regime in ("day", "night"):
            card = store.gate_scorecard("live", "yolo-serial", regime=regime, **kw)
            # The list length IS the count — the invariant the missed-visit spec
            # exists to preserve, held under scoping too.
            assert len(card["missed_visits"]) == card["visits"]["wholly_missed"]
    finally:
        store.close()


def test_regime_requires_a_classifier_and_rejects_a_bad_value(tmp_path):
    store = _store(tmp_path)
    try:
        _scorecard_fixture(store)
        # Scoping to a regime with no way to tell day from night is incoherent —
        # fail loudly rather than silently answer with everything's numbers.
        with pytest.raises(ValueError, match="is_night"):
            store.gate_scorecard("live", "yolo-serial", regime="night", **_SC_KW)
        with pytest.raises(ValueError, match="regime"):
            store.gate_scorecard(
                "live", "yolo-serial", regime="dusk",
                is_night=lambda ts: False, **_SC_KW,
            )
    finally:
        store.close()
